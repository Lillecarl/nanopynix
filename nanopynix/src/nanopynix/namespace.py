"""Run a worker in its own user namespace, over an overlay Nix store.

A worker that enters a user namespace can mount an OverlayFS at ``/nix/store``
whose lower layer is the host store and whose upper layer is its own scratch
directory. The worker then builds against every path the host already has,
copies nothing, and writes only into the upper layer. The host store does not
change, because the mount lives in a private mount namespace.

That combination is what makes an *impure* build practical. The Nix daemon owns
the sandbox settings of the host store, so a client cannot relax them. A local
overlay store is a store of our own, so this worker sets its own ``sandbox``
and ``sandbox-paths``. The results go back to the host store afterwards, over
the daemon protocol, with an ordinary copy.

Why this happens at worker start
--------------------------------
``unshare(CLONE_NEWUSER)`` fails with ``EINVAL`` in a process that has more
than one thread. The worker has an event loop and a Nix executor thread as soon
as it can answer an RPC, so a "enter a namespace now" RPC cannot work. A
``fork`` keeps only the calling thread, so the forkserver child *is*
single-threaded, and :func:`enter_overlay_namespace` runs there, before the
worker starts a thread of its own. :func:`nanopynix.rpc.worker.worker_service_factory`
is the last single-threaded point, and it is where the call goes.

For the same reason nothing in this module may move to a thread.
``anyio.to_thread.run_sync`` would start the second thread that makes the
``unshare`` fail. These calls are blocking syscalls that must run on the
calling thread, and they run once, at start.

Also note that the configuration cannot travel in an environment variable. The
forkserver takes a copy of the environment when it starts, and it reuses that
copy for every later child, so a variable set after that point never arrives.
The spec travels as a pickled argument instead.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import multiprocessing
import os
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from grpclib_transports.multiprocessing import main_module_not_reexecuted

from nanopynix._typechecking import BEARTYPING
from nanopynix.stores import LocalOverlay

# Not a plain `if TYPE_CHECKING:` block: beartype resolves this annotation at
# call time, and under a TYPE_CHECKING-only import it cannot. It then raises
# BeartypeCallHintForwardRefException *inside the probe child*, which dies
# before it can report, and the probe reads that as "unsupported".
if TYPE_CHECKING or BEARTYPING:
    from multiprocessing.connection import Connection

#: ``mount(2)`` flags. Python has no binding for ``mount``, so this module
#: calls libc and spells the flags itself.
_MS_REC = 0x4000
_MS_PRIVATE = 1 << 18

#: The store directory the overlay is mounted at. Not configurable, and that
#: is the point: mounting the merged store anywhere else makes it a *relocated*
#: store, whose paths need :mod:`nanopynix.store_exec` to run. At the logical
#: location every path in the closure is already correct.
STORE_DIR = "/nix/store"

#: Nix names this feature experimental, and refuses the store URI without it.
EXPERIMENTAL_FEATURE = "local-overlay-store"

#: How long :func:`probe_namespace_support` waits for its child. The child does
#: a handful of syscalls, so this bounds a hang rather than slow work.
_PROBE_TIMEOUT_SECONDS = 30.0


def _libc() -> ctypes.CDLL:
    name = ctypes.util.find_library("c")
    if name is None:
        raise RuntimeError("cannot locate libc, so the overlay mount is not possible")
    return ctypes.CDLL(name, use_errno=True)


def _mount(source: str, target: str, fstype: str | None, flags: int, data: str | None) -> None:
    """Call ``mount(2)``, and raise ``OSError`` with the real errno on failure."""

    def encode(value: str | None) -> bytes | None:
        return None if value is None else value.encode()

    result = _libc().mount(
        encode(source),
        encode(target),
        encode(fstype),
        ctypes.c_ulong(flags),
        encode(data),
    )
    if result != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"mount({source!r}, {target!r}, {fstype!r}): {os.strerror(errno)}")


@dataclass(frozen=True)
class OverlayNamespace:
    """Where the layers of one worker's overlay store live.

    Every field is a plain ``str`` rather than a ``Path`` because this object
    is pickled through the forkserver to reach the worker.

    ``work_dir`` must be on the same filesystem as ``upper_dir``, and must not
    be inside it. OverlayFS requires both.
    """

    upper_dir: str
    work_dir: str
    state_dir: str
    log_dir: str
    #: Store URI for the lower store. It supplies the *metadata* of the paths
    #: the lower layer supplies as bytes, so it must be the host store: the
    #: mount check below compares its store directory against ``lowerdir``.
    lower_store: str = "daemon"

    @classmethod
    def under(cls, root: str | os.PathLike[str], *, lower_store: str = "daemon") -> OverlayNamespace:
        """Lay the four directories out under one *root*."""
        base = Path(root)
        return cls(
            upper_dir=str(base / "upper"),
            work_dir=str(base / "work"),
            state_dir=str(base / "state"),
            log_dir=str(base / "log"),
            lower_store=lower_store,
        )

    def store_config(self) -> LocalOverlay:
        """Return the typed store configuration this layout names.

        ``work_dir`` is absent on purpose. OverlayFS needs it, and Nix does
        not: it is a requirement of the mount, which is this class's concern,
        not a setting of the store.
        """
        return LocalOverlay(
            lower_store=self.lower_store,
            upper_layer=self.upper_dir,
            state=self.state_dir,
            log=self.log_dir,
        )

    def store_uri(self) -> str:
        """Return the ``local-overlay://`` URI that names this store.

        Rendered by :mod:`nanopynix.stores`, so this URI is spelled and escaped
        exactly like every other store URI the library produces, and Nix's own
        parser is what validates it.
        """
        return self.store_config().uri()

    def required_settings(self) -> dict[str, str]:
        """Return the Nix settings this store cannot work without.

        ``build-users-group`` is the one that is not obvious. As the only
        mapped user, the worker is root inside its namespace, so Nix tries to
        give the store directory to the build-users group and gets ``EINVAL``,
        because that group has no mapping here. An empty value tells Nix to
        build as itself. Nix's own tests for this store type set the same two
        values.
        """
        return {
            "build-users-group": "",
            "require-drop-supplementary-groups": "false",
        }


@dataclass(frozen=True)
class NamespaceSupport:
    """What :func:`probe_namespace_support` found on this host."""

    supported: bool
    #: Empty when supported. Otherwise one sentence that names the step that
    #: failed, ready to show to a user.
    reason: str = ""

    def __bool__(self) -> bool:
        return self.supported


def _probe_child(upper_root: str, conn: Connection) -> None:
    """Do the real thing in a throwaway process, and report what happened.

    Every mount here lives in this process's own namespace and disappears when
    it exits, so the probe cannot change the host.
    """
    try:
        probe_upper = f"{upper_root}/probe-upper"
        probe_work = f"{upper_root}/probe-work"
        probe_mount = f"{upper_root}/probe-mnt"
        spec = OverlayNamespace(
            upper_dir=probe_upper,
            work_dir=probe_work,
            state_dir=f"{upper_root}/probe-state",
            log_dir=f"{upper_root}/probe-log",
        )
        # Everything enter_overlay_namespace does, except that the overlay
        # goes over a scratch directory rather than over /nix/store. The
        # layers are on the filesystem the real run would use, which is what
        # decides whether OverlayFS and user extended attributes work.
        enter_overlay_namespace(spec, mount_at=probe_mount)
        conn.send("")
    except Exception as exc:
        conn.send(str(exc))
    finally:
        conn.close()


def probe_namespace_support(root: str | os.PathLike[str] | None = None) -> NamespaceSupport:
    """Report whether this host can run an overlay-store worker.

    Give *root* the directory the real upper layer will live under, so the
    probe tests the same filesystem. Without it the probe uses a temporary
    directory, which can give a different answer: OverlayFS needs the upper
    layer to support user extended attributes, and not every filesystem does.

    This runs the actual sequence in a throwaway forkserver child rather than
    reading capability flags. A host can have user namespaces enabled and
    still fail to mount, so only the real calls give a reliable answer. The
    child's namespace dies with it, so nothing on the host changes.

    Call it before starting a namespaced worker. Without it the worker fails
    inside the forkserver child, and the caller sees a dead worker instead of
    the reason.
    """
    if sys.platform != "linux":
        return NamespaceSupport(False, f"an overlay store namespace needs Linux, not {sys.platform}")

    if root is not None:
        # The caller is about to put the real layers here, so creating it now
        # costs nothing and lets the probe measure the right filesystem.
        Path(root).mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="nanopynix-nsprobe-", dir=root) as probe_root:
        context = multiprocessing.get_context("forkserver")
        parent_conn, child_conn = context.Pipe()
        process = context.Process(target=_probe_child, args=(probe_root, child_conn))
        # The same reason the worker spawn needs it, and a worse symptom here:
        # a child that dies while it runs the `__main__` of the program is
        # read below as "the probe process died", so a host that supports an
        # overlay namespace is reported as a host that does not.
        with main_module_not_reexecuted():
            process.start()
        child_conn.close()
        try:
            reason = parent_conn.recv()
        except EOFError:
            reason = "the probe process died before it reported a result"
        finally:
            parent_conn.close()
            process.join(_PROBE_TIMEOUT_SECONDS)
            if process.exitcode is None:
                process.kill()
                process.join()

    return NamespaceSupport(not reason, reason)


def enter_overlay_namespace(spec: OverlayNamespace, *, mount_at: str = STORE_DIR) -> None:
    """Enter a private user and mount namespace, and mount the overlay store.

    Call this on the worker's only thread, before the worker starts any other
    thread, and before Nix opens a store. It returns with ``/nix/store``
    replaced, for this process and its children alone.
    """
    if sys.platform != "linux":
        raise RuntimeError(f"an overlay store namespace needs Linux, not {sys.platform}")

    threads = threading.active_count()
    if threads != 1:
        raise RuntimeError(
            f"enter_overlay_namespace needs a single-threaded process, but this one has {threads} threads. "
            "unshare(CLONE_NEWUSER) fails with EINVAL once a second thread exists, so this must run at "
            "worker start rather than from an RPC."
        )

    uid = os.getuid()
    gid = os.getgid()

    try:
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
    except OSError as exc:
        raise RuntimeError(
            f"cannot create a user namespace ({exc.strerror}). Unprivileged user namespaces are "
            "disabled on this host, so an overlay store worker is not possible here."
        ) from exc

    # Map this user to root inside the namespace, and nothing else. Root is
    # what lets Nix build without a build-users group. `setgroups=deny` is a
    # precondition the kernel puts on writing `gid_map` unprivileged.
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"0 {uid} 1")
    Path("/proc/self/gid_map").write_text(f"0 {gid} 1")

    # Detach from the mount propagation of the parent before touching
    # /nix/store. Without this step the overlay travels back out on a host
    # where / is shared, which is the systemd default, and the host store
    # changes for every process on the machine.
    _mount("none", "/", None, _MS_REC | _MS_PRIVATE, None)

    for directory in (spec.upper_dir, spec.work_dir, spec.state_dir, spec.log_dir):
        Path(directory).mkdir(parents=True, exist_ok=True)
    if mount_at != STORE_DIR:
        # Only the probe takes this branch. /nix/store always exists.
        Path(mount_at).mkdir(parents=True, exist_ok=True)

    # `userxattr` is required, not a refinement. An unprivileged mount cannot
    # write the `trusted.overlay.*` attributes that OverlayFS uses by default,
    # so deleting a lower-layer path would fail when it writes the whiteout.
    #
    # `lowerdir` is the same directory this mounts over. OverlayFS resolves the
    # lower layer before the new mount hides it, so the host store stays
    # readable underneath.
    options = f"lowerdir={STORE_DIR},upperdir={spec.upper_dir},workdir={spec.work_dir},userxattr"
    try:
        _mount("overlay", mount_at, "overlay", 0, options)
    except OSError as exc:
        raise RuntimeError(
            f"cannot mount the overlay store at {mount_at} ({exc.strerror}). "
            f"Check that {spec.work_dir} is on the same filesystem as {spec.upper_dir}, "
            "and that the filesystem holding them supports user extended attributes."
        ) from exc
