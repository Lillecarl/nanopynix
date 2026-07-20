"""Hermetic LocalStore and native-daemon fixtures for integration tests."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import os
import shutil
import signal
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import nanopynix

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from nanopynix.models import StorePath

    RpcSessionFactory = Callable[..., nanopynix.Session]
    InprocSessionFactory = Callable[..., nanopynix.inproc.Session]


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

    @property
    def settings(self) -> nanopynix.NixSettings:
        return nanopynix.NixSettings(
            build_users_group="",
            require_drop_supplementary_groups=False,
        )

    def rpc_session(self, **kwargs: Any) -> nanopynix.Session:
        return nanopynix.Session(
            store_uri=self.store_uri,
            load_config=False,
            settings=self.settings,
            **kwargs,
        )

    def inproc_session(self, **kwargs: Any) -> nanopynix.inproc.Session:
        return nanopynix.inproc.Session(
            store_uri=self.store_uri,
            load_config=False,
            settings=self.settings,
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
        """
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


async def _force_rmtree(path: Path) -> None:
    """Remove a closed test root even if Nix made entries read-only."""

    def onexc(function: Callable[..., object], raw_path: str, _exc: BaseException) -> None:
        failed_path = Path(raw_path)
        for path_to_fix in (failed_path, failed_path.parent):
            with contextlib.suppress(FileNotFoundError):
                path_to_fix.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        function(raw_path)

    for attempt in range(20):
        if not await anyio.Path(path).exists():
            return
        try:
            shutil.rmtree(path, onexc=onexc)
        except OSError as error:
            if error.errno != errno.ENOTEMPTY or attempt == 19:
                raise
            # A Nix worker can finish releasing a store entry just after its
            # RPC shutdown acknowledgement. Retry its one filesystem race
            # rather than leaving pytest's managed temporary root behind.
            await asyncio.sleep(0.05)
        else:
            return


_PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:
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
            env=daemon_environment,
            start_new_session=True,
            preexec_fn=_die_with_parent,
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
                f"log:\n{daemon_log_path.read_text(errors='replace')}"
            )
        await asyncio.sleep(0.05)
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
            f"without creating {root / 'nix' / 'store'}; log:\n{daemon_log_path.read_text(errors='replace')}"
        )
    return _Daemon(process, socket_path)


async def _environment(backend: str, root: Path) -> tuple[NixTestEnvironment, _Daemon | None]:
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
        await _force_rmtree(root)


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
            runner.run(_force_rmtree(root))


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
    environment, daemon = await _environment(nix_backend, root)
    try:
        yield environment
    finally:
        if daemon is not None:
            await daemon.close()
        await _force_rmtree(root)


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
