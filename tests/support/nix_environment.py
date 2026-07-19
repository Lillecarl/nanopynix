"""Hermetic LocalStore and native-daemon fixtures for integration tests."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import signal
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import nanopynix
from nanopynix.models import StorePath

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

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


async def _force_rmtree(path: Path) -> None:
    """Remove a closed test root even if Nix made entries read-only."""

    def onexc(function: Callable[..., object], raw_path: str, _exc: BaseException) -> None:
        failed_path = Path(raw_path)
        for path_to_fix in (failed_path, failed_path.parent):
            with contextlib.suppress(FileNotFoundError):
                path_to_fix.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        function(raw_path)

    for attempt in range(20):
        if not path.exists():
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


async def _start_daemon(root: Path) -> _Daemon:
    socket_path = root / "nix" / "var" / "nix" / "daemon-socket" / "socket"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    daemon_environment = os.environ.copy()
    daemon_environment["NIX_DAEMON_SOCKET_PATH"] = str(socket_path)
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
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    for _ in range(100):
        if socket_path.is_socket():
            return _Daemon(process, socket_path)
        if process.returncode is not None:
            raise RuntimeError(f"temporary nix daemon exited with status {process.returncode}")
        await asyncio.sleep(0.05)
    process.terminate()
    await process.wait()
    raise RuntimeError(f"temporary nix daemon did not create {socket_path}")


async def _environment(backend: str, root: Path) -> tuple[NixTestEnvironment, _Daemon | None]:
    if backend == "local":
        return NixTestEnvironment(backend=backend, root=root, store_uri=f"local?root={root}"), None
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
def rpc_session(isolated_nix_environment: NixTestEnvironment) -> RpcSessionFactory:
    """Create RPC sessions against this test's isolated Store."""
    return isolated_nix_environment.rpc_session


@pytest.fixture
def inproc_session(isolated_nix_environment: NixTestEnvironment) -> InprocSessionFactory:
    """Create in-process sessions against this test's isolated Store."""
    return isolated_nix_environment.inproc_session


@pytest.fixture
async def seeded_store_path(isolated_nix_environment: NixTestEnvironment) -> StorePath:
    """A known-valid content-addressed path, never borrowed from the host store."""
    environment = isolated_nix_environment
    source = environment.root.parent / "fixture.txt"
    source.write_text("nanopynix hermetic fixture\n", encoding="utf-8")
    async with environment.rpc_session() as nix, nix.store() as store:
        return await store.add_to_store(str(source), name="nanopynix-test-fixture", method="flat")
