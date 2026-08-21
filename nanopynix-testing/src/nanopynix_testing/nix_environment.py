"""Hermetic local-store and native-daemon fixtures for integration tests."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import os
import shutil
import signal
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import nanopynix
from nanopynix.settings import DEFAULT_EXPERIMENTAL_FEATURES

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from nanopynix.models import StorePath

    RpcSessionFactory = Callable[..., nanopynix.rpc.Session]
    InprocSessionFactory = Callable[..., nanopynix.inproc.Session]

# Tries at removing a temporary store root. A Nix worker can finish releasing a
# store entry just after its shutdown acknowledgement, so `ENOTEMPTY` here is a
# race and not a failure. Twenty tries at 0.05 s is one second, which is longer
# than any release measured.
_RMTREE_ATTEMPTS = 20

# **The switch that puts the suite on the store of the machine.**
#
# Every store this suite makes is a chroot store: `root=` moves the physical
# store under a temporary directory and leaves the logical store dir at
# `/nix/store`. Nix builds in such a store on Linux alone. It answers the
# difference with a mount namespace and a bind mount, and
# `derivation-builder.cc` throws "building using a diverted store is not
# supported on this platform" where there is no namespace to use. So every
# test that builds a derivation fails on macOS, for a reason no test owns.
#
# This variable makes the fixtures open the store of the machine instead. That
# store is not relocated, so a build there is a plain build and it works on
# every platform.
#
# **It is off by default, and the default is the one a developer wants.** A
# hermetic store leaves the machine alone; the store of the machine is shared,
# and a test that writes it leaves a path behind. `NANOPYNIX_TEST_DELETE_PATHS_FILE`
# and `StorePathRecorder` record each such path, and the CI step deletes them
# after the run.
#
# **A multi-user installation answers this only through the daemon.**
# `/nix/var/nix/db` belongs to root there, so a direct `local://` store cannot
# take the big lock and every write fails with `Permission denied`. Measured on
# macOS 26.5.1, and the runner of the macOS job takes the multi-user installer
# as well. Use `--nix-test-backends daemon` with this variable on such a host.
SYSTEM_STORE_ENV = "NANOPYNIX_TEST_SYSTEM_STORE"


def use_system_store() -> bool:
    """Whether the fixtures open the store of the machine, not a chroot store."""
    return os.environ.get(SYSTEM_STORE_ENV, "") not in ("", "0")


@dataclass
class _Daemon:
    process: asyncio.subprocess.Process
    socket_path: Path

    async def close(self) -> None:
        if self.process.returncode is None:
            # ``nix daemon`` forks a connection worker. It shares this
            # dedicated process group, so closing the fixture cannot leave a
            # worker holding files in the private store. This never targets
            # the system daemon's process group.
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except TimeoutError:
                os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()


@dataclass(frozen=True)
class NixTestEnvironment:
    """One hermetic test-store endpoint and its deterministic Nix settings."""

    backend: str
    root: Path
    store_uri: str
    # Whether `store_uri` names a chroot store under `root`. False means the
    # store of the machine, which this suite does not own. `root` stays a
    # directory of pytest either way, so the teardown that removes it is safe
    # in both modes and no caller has to ask which mode it is in.
    relocated: bool = True

    @property
    def settings(self) -> nanopynix.NixSettings:
        return nanopynix.NixSettings(
            build_users_group="",
            require_drop_supplementary_groups=False,
            # "daemon" (the running system nix-daemon, if any) first, falling
            # through to the normal binary cache -- lets these hermetic,
            # from-scratch stores substitute anything the host machine has
            # already realized (e.g. this repo's own dev-shell closure)
            # straight off the local Unix socket instead of a network fetch,
            # cutting a from-scratch `hello` build's substitution time from
            # ~1.9s to ~1.6s in measurement, with paths NOT locally present
            # falling through to cache.nixos.org exactly as before. Safe when
            # no daemon is running too: Nix logs one connection-refused error
            # and moves on to the next substituter, it does not retry per
            # path or block the fallback (measured: same ~1.9s either way).
            substituters=["daemon", "https://cache.nixos.org"],
        )

    def rpc_session(self, **kwargs: Any) -> nanopynix.rpc.Session:
        return nanopynix.rpc.Session(
            store_uri=kwargs.pop("store_uri", self.store_uri),
            load_config=kwargs.pop("load_config", False),
            settings=kwargs.pop("settings", self.settings),
            **kwargs,
        )

    def inproc_session(self, **kwargs: Any) -> nanopynix.inproc.Session:
        return nanopynix.inproc.Session(
            store_uri=kwargs.pop("store_uri", self.store_uri),
            load_config=kwargs.pop("load_config", False),
            settings=kwargs.pop("settings", self.settings),
            **kwargs,
        )

    def pynix_store_args(self) -> list[str]:
        return ["--store", self.store_uri]

    def physical_path(self, store_path: str) -> Path:
        """Map a logical ``/nix/store/...`` path to its real on-disk location.

        A ``root=`` param relocates the physical store underneath ``self.root``
        (Nix reports ``storeDir`` as the logical ``/nix/store`` regardless, see
        ``realStoreDir``), so reading a realized path directly -- bypassing the
        daemon protocol -- needs this translation instead of the raw string.

        The store of the machine is not relocated, so the logical path is
        already the path on disk and the translation must not happen.
        """
        if not self.relocated:
            return Path(store_path)
        return self.root / store_path.removeprefix("/")

    def store_uri_matches(self, uri: str) -> bool:
        """Whether ``uri`` is this environment's store, as reported by an open Store.

        An open unix:// store is not required to echo back byte-identical to
        the configured URI: Nix's own ``UDSRemoteStoreConfig::getReference()``
        may collapse it to the bare "daemon" shorthand (when the socket path
        happens to equal the connecting process's live default), and older
        Nix versions have been observed to drop the "root" query param
        entirely when reporting an open store's URI. Both are real,
        Nix-version-dependent behaviors outside this fixture's control, so a
        scheme-only check is what's actually reliable across versions.
        """
        if uri == self.store_uri:
            return True
        if self.backend != "daemon":
            return False
        return uri == "daemon" or uri.startswith(("daemon?", "unix://"))


def with_nixpkgs(source: str, nixpkgs_path: str) -> str:
    """Substitute a literal ``<nixpkgs>`` in a Nix expression for a hermetic path."""
    return source.replace("<nixpkgs>", nixpkgs_path)


async def force_rmtree(path: Path) -> None:
    """Remove a closed test root even if Nix made entries read-only."""

    def onexc(function: Callable[..., object], raw_path: str, _exc: BaseException) -> None:
        failed_path = Path(raw_path)
        for path_to_fix in (failed_path, failed_path.parent):
            with contextlib.suppress(FileNotFoundError):
                path_to_fix.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        function(raw_path)

    for attempt in range(_RMTREE_ATTEMPTS):
        if not await anyio.Path(path).exists():
            return
        try:
            shutil.rmtree(path, onexc=onexc)
        except OSError as error:
            if error.errno != errno.ENOTEMPTY or attempt == _RMTREE_ATTEMPTS - 1:
                raise
            # A Nix worker can finish releasing a store entry just after its
            # RPC shutdown acknowledgement. Retry its one filesystem race
            # rather than leaving pytest's managed temporary root behind.
            await anyio.sleep(0.05)
        else:
            return


_PR_SET_PDEATHSIG = 1


# pyright reads `sys.platform` statically, so off Linux the branch below that
# names this function is dead code and `reportUnusedFunction` fires. The
# function is used, on the platform that has the call it makes.
def _die_with_parent() -> None:  # pyright: ignore[reportUnusedFunction] -- see the note above, and `_PDEATHSIG_PREEXEC` below
    """Ask the kernel to SIGKILL this process the instant pytest's process dies.

    ``_Daemon.close`` only runs when pytest's own fixture teardown chain gets
    to execute. A hard kill, crash, or external interrupt of the pytest
    process skips that entirely, and this ``start_new_session=True`` child
    lives in its own process group precisely so pytest's teardown can
    ``killpg`` its connection workers -- which also means signals sent to a
    dying pytest never reach it on their own. ``PR_SET_PDEATHSIG`` closes that
    gap unconditionally, regardless of how the parent goes away.
    """
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


# **`PR_SET_PDEATHSIG` is Linux, and macOS has nothing to put in its place.**
# The call above names `libc.so.6`, which does not exist on macOS, and `prctl`,
# which is a Linux system call. `subprocess` runs this in the child between
# `fork` and `exec`, so the failure arrived as
# `SubprocessError: Exception occurred in preexec_fn`, and every test on the
# daemon backend failed at fixture setup. It was 21 of the 44 failures of the
# first full macOS run.
#
# The alternatives do not reach. `kqueue` with `EVFILT_PROC` and `NOTE_EXIT`
# watches a parent, but it needs a loop in the child, and the child here is
# `nix daemon`, which runs its own code. A pipe that closes on parent exit
# needs the child to read it, which that daemon does not do either.
#
# **So the safety net is absent on macOS, and this says what that costs.**
# `_Daemon.close` still ends the daemon on every ordinary teardown. What is
# gone is the guarantee for a pytest that dies without teardown, such as a
# `SIGKILL`. That leaves one `nix daemon` per abandoned run, holding a socket
# under a temporary directory of pytest.
_PDEATHSIG_PREEXEC = _die_with_parent if sys.platform == "linux" else None


async def _start_daemon(root: Path) -> _Daemon:
    socket_path = root / "nix" / "var" / "nix" / "daemon-socket" / "socket"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    daemon_environment = os.environ.copy()
    daemon_environment["NIX_DAEMON_SOCKET_PATH"] = str(socket_path)
    # Captured (rather than discarded) so a lazy-init failure below has the
    # daemon's own account of what it did, not just the client-side symptom.
    daemon_log_path = root / "nix-daemon.log"
    daemon_log = daemon_log_path.open("wb")
    try:
        process = await asyncio.create_subprocess_exec(
            "nix",
            "daemon",
            "--store",
            f"local://{root}",
            "--option",
            "build-users-group",
            "",
            "--option",
            "require-drop-supplementary-groups",
            "false",
            # Stated explicitly, from the same tuple the client uses, because
            # otherwise this daemon reads the *host's* nix.conf for them. The
            # client is already hermetic (`load_config=False` plus
            # `NixSettings.experimental_features`), so leaving the daemon to
            # the host makes the pair disagree on any host that enables a
            # different set: `ca-derivations` is in `/etc/nix/nix.conf` on a
            # typical NixOS dev box but not on a GitHub runner, where
            # install-nix-action writes only "nix-command flakes" -- which is
            # exactly why `test_*_read_derivation_keeps_nested_input_drvs`
            # passed locally and failed in CI, on the daemon backend alone.
            # A command-line --option outranks any config file, so this is
            # deterministic wherever the suite runs.
            #
            # Deliberately narrow: substituters are NOT pinned here, so this
            # daemon keeps inheriting the host's (the local daemon socket, a
            # cachix cache in CI, ...). Substitution is the one host coupling
            # these hermetic stores are meant to keep -- see
            # `NixTestEnvironment.settings`.
            "--option",
            "experimental-features",
            " ".join(DEFAULT_EXPERIMENTAL_FEATURES),
            env=daemon_environment,
            start_new_session=True,
            preexec_fn=_PDEATHSIG_PREEXEC,
            stdout=daemon_log,
            stderr=asyncio.subprocess.STDOUT,
        )
    finally:
        daemon_log.close()
    for _ in range(100):
        if socket_path.is_socket():
            break
        if process.returncode is not None:
            raise RuntimeError(
                f"temporary nix daemon exited with status {process.returncode}; "
                f"log:\n{daemon_log_path.read_text(errors='replace')}",
            )
        await anyio.sleep(0.05)
    else:
        process.terminate()
        await process.wait()
        raise RuntimeError(f"temporary nix daemon did not create {socket_path}")

    # The socket accepts connections before the daemon has laid out the store
    # directory on disk (nix/store, var/nix/db, ...) -- that only happens
    # lazily, on the first real store operation. Some client code paths (seen
    # in CI only, never locally) read store-local files directly rather than
    # going through the daemon protocol, so they never trigger that lazy
    # init and see a bare ENOENT instead. Force it here, via an operation
    # confirmed to reliably trigger it, before any test can race it.
    warmup = await asyncio.create_subprocess_exec(
        "nix",
        "store",
        "info",
        "--store",
        f"unix://{socket_path}?root={root}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    warmup_status = await warmup.wait()
    if warmup_status != 0 or not (root / "nix" / "store").is_dir():
        raise RuntimeError(
            f"temporary nix daemon warmup (nix store info) exited {warmup_status} "
            f"without creating {root / 'nix' / 'store'}; log:\n{daemon_log_path.read_text(errors='replace')}",
        )
    return _Daemon(process, socket_path)


async def _environment(
    backend: str,
    root: Path,
    *,
    allow_system_store: bool = True,
) -> tuple[NixTestEnvironment, _Daemon | None]:
    """Build one store endpoint.

    ``allow_system_store`` is False for the fixture that owns a store of its
    own. ``isolated_nix_environment`` is that fixture, and its docstring gives
    the rule: a test that mutates the whole store takes it. Such a test calls
    ``collect_garbage(DELETE_DEAD)``, ``optimise_store`` or ``verify_store``,
    and each of those acts on every path of the store it is given. Pointing one
    at the store of the machine would delete the paths of that machine, so the
    switch must not reach this fixture.
    """
    if allow_system_store and use_system_store():
        return _system_store_environment(backend, root), None
    if backend == "local":
        # "local://" (not "local") -- Nix always adds the "//" authority
        # separator when it reports an open store's URI back, so starting
        # from the same canonical form makes it round-trip exactly.
        return NixTestEnvironment(backend=backend, root=root, store_uri=f"local://?root={root}"), None
    if backend == "daemon":
        daemon = await _start_daemon(root)
        return (
            NixTestEnvironment(backend=backend, root=root, store_uri=f"unix://{daemon.socket_path}?root={root}"),
            daemon,
        )
    raise ValueError(f"unknown Nix test backend: {backend!r}")


def _system_store_environment(backend: str, root: Path) -> NixTestEnvironment:
    """The store of the machine, for a host that cannot build in a chroot store.

    ``root`` stays the scratch directory of pytest. It no longer holds the
    store, and the fixtures still own it and still remove it.

    **The daemon backend names the daemon of the machine, and starts none.**
    ``_start_daemon`` exists to give a chroot store its own daemon. Here the
    daemon that owns the store is already running, so a second one would open
    the same database from a second process for no gain.
    """
    if backend == "local":
        store_uri = "local://"
    elif backend == "daemon":
        store_uri = "daemon"
    else:
        raise ValueError(f"unknown Nix test backend: {backend!r}")
    return NixTestEnvironment(backend=backend, root=root, store_uri=store_uri, relocated=False)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """The process fixture needs a stable asyncio loop for subprocess ownership."""
    return "asyncio"


@pytest.fixture(scope="session")
async def shared_nix_environment(
    nix_backend: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[NixTestEnvironment]:
    """A backend-specific root shared only by tests that request this fixture."""
    root = tmp_path_factory.mktemp(f"nix-{nix_backend}-shared")
    environment, daemon = await _environment(nix_backend, root)
    try:
        yield environment
    finally:
        if daemon is not None:
            await daemon.close()
        await force_rmtree(root)


@pytest.fixture(scope="session")
def l1_nix_environment(
    nix_backend: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[NixTestEnvironment]:
    """Sync counterpart of ``shared_nix_environment`` for plain (non-async) L1
    binding tests, which cannot depend on an async fixture. Bridges via
    ``asyncio.run`` only because starting the native daemon subprocess must
    stay async; nothing below actually needs a running event loop.
    """
    root = tmp_path_factory.mktemp(f"nix-{nix_backend}-l1")
    # A subprocess transport stays bound to the loop that created it, so
    # setup/teardown must share one loop rather than a fresh asyncio.run()
    # each time (an ``asyncio.run(daemon.close())`` on its own loop rejects
    # the ``process.wait()`` future as belonging to a different loop).
    with asyncio.Runner() as runner:
        environment, daemon = runner.run(_environment(nix_backend, root))
        try:
            yield environment
        finally:
            if daemon is not None:
                runner.run(daemon.close())
            runner.run(force_rmtree(root))


@pytest.fixture
async def isolated_nix_environment(
    nix_backend: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[NixTestEnvironment]:
    """A fresh root for one test, including a separate daemon when requested."""
    # Do not put this beneath a test's ``tmp_path``. Tests frequently evaluate
    # that entire directory as a path flake, and Nix rejects the daemon socket
    # as an unsupported source file type. ``tmp_path_factory`` still gives
    # pytest ownership of this distinct per-test root.
    root = tmp_path_factory.mktemp(f"nix-{nix_backend}")
    # Always a chroot store, even with `NANOPYNIX_TEST_SYSTEM_STORE` set. See
    # `_environment` for the reason: this is the fixture of the tests that
    # collect garbage.
    environment, daemon = await _environment(nix_backend, root, allow_system_store=False)
    try:
        yield environment
    finally:
        if daemon is not None:
            await daemon.close()
        await force_rmtree(root)


@pytest.fixture
def rpc_session(shared_nix_environment: NixTestEnvironment) -> RpcSessionFactory:
    """Create RPC sessions against this backend's shared Store.

    Most tests only need a Store to exist, not one exclusive to them; sharing
    one daemon per backend keeps the suite from paying a fresh ``nix daemon``
    spin-up per test. Tests that mutate shared state (destructive GC, daemon
    lifecycle) should depend on ``isolated_nix_environment`` directly instead.
    """
    return shared_nix_environment.rpc_session


@pytest.fixture
def inproc_session(shared_nix_environment: NixTestEnvironment) -> InprocSessionFactory:
    """Create in-process sessions against this backend's shared Store. See ``rpc_session``."""
    return shared_nix_environment.inproc_session


@pytest.fixture
async def seeded_store_path(shared_nix_environment: NixTestEnvironment) -> StorePath:
    """A known-valid content-addressed path, never borrowed from the host store.

    Depends on ``shared_nix_environment`` to match the store ``rpc_session``/
    ``inproc_session`` connect to by default; adding this fixed content is
    idempotent, so many tests requesting it concurrently is safe.
    """
    environment = shared_nix_environment
    source = environment.root.parent / "fixture.txt"
    source.write_text("nanopynix hermetic fixture\n", encoding="utf-8")
    async with environment.rpc_session() as nix, nix.store() as store:
        return await store.add_to_store(str(source), name="nanopynix-test-fixture", method="flat")
