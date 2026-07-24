"""Integration coverage for the isolated native-daemon supervisor."""

from __future__ import annotations

import asyncio
import os
import socket
from typing import TYPE_CHECKING

import pytest

from nanopynix.rpc.daemon import DaemonConfig, DaemonSupervisor

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skip(
    reason="nanopynix.rpc.daemon ('pump the nix daemon from Python') is unfinished "
    "lab/prototype work, not a supported feature -- per author, this should have "
    "stayed on a throwaway branch.",
)


async def _wait_until(predicate: object) -> None:
    """Poll an async predicate until it is truthy, failing the test after 5s."""
    try:
        async with asyncio.timeout(5.0):
            while not await predicate():  # type: ignore[operator] -- predicate is always an async callable  # noqa: ASYNC110 -- polls an arbitrary caller-supplied predicate; no asyncio.Event exists to wait on instead
                await asyncio.sleep(0.05)
    except TimeoutError:
        pytest.fail("condition was not met before timeout")


@pytest.mark.anyio
async def test_supervisor_serves_nix_protocol_from_isolated_connection_worker(tmp_path: Path) -> None:
    socket_path = tmp_path / "daemon.sock"
    config = DaemonConfig(store_uri=f"local?root={tmp_path / 'store'}")

    async with DaemonSupervisor(socket_path, config):
        process = await asyncio.create_subprocess_exec(
            "nix",
            "store",
            "ping",
            "--store",
            f"unix://{socket_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()

    assert process.returncode == 0, stderr.decode()
    assert not socket_path.exists()


@pytest.mark.anyio
async def test_supervisor_forgets_connection_after_client_disconnects(tmp_path: Path) -> None:
    """A finished client must not leave its worker counted as active."""
    socket_path = tmp_path / "daemon.sock"
    config = DaemonConfig(store_uri=f"local?root={tmp_path / 'store'}")

    async with DaemonSupervisor(socket_path, config) as supervisor:
        process = await asyncio.create_subprocess_exec(
            "nix",
            "store",
            "ping",
            "--store",
            f"unix://{socket_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()

        async def _no_active_connections() -> bool:
            return supervisor.active_connections == 0

        await _wait_until(_no_active_connections)
        assert not supervisor._connection_tasks  # type: ignore[reportPrivateUsage] -- verifying internal task bookkeeping is reaped


@pytest.mark.anyio
async def test_supervisor_close_terminates_a_stalled_connection_worker(tmp_path: Path) -> None:
    """close() must reap a worker that is still blocked mid-handshake, not hang forever."""
    socket_path = tmp_path / "daemon.sock"
    config = DaemonConfig(store_uri=f"local?root={tmp_path / 'store'}")

    supervisor = DaemonSupervisor(socket_path, config)
    await supervisor.start()

    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.setblocking(False)
    loop = asyncio.get_running_loop()
    await loop.sock_connect(raw, str(socket_path))
    try:

        async def _has_active_connection() -> bool:
            return supervisor.active_connections > 0

        await _wait_until(_has_active_connection)

        await asyncio.wait_for(supervisor.close(), timeout=10)

        assert supervisor.active_connections == 0
        assert not socket_path.exists()
    finally:
        raw.close()


@pytest.mark.anyio
async def test_connection_worker_owns_a_dedicated_process_group(tmp_path: Path) -> None:
    """Each worker's own session lets close() killpg its Nix descendants without
    touching the supervisor's own process group."""
    socket_path = tmp_path / "daemon.sock"
    config = DaemonConfig(store_uri=f"local?root={tmp_path / 'store'}")

    async with DaemonSupervisor(socket_path, config) as supervisor:
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(raw, str(socket_path))
        try:

            async def _has_active_connection() -> bool:
                return supervisor.active_connections > 0

            await _wait_until(_has_active_connection)

            children = tuple(supervisor._children)  # type: ignore[reportPrivateUsage] -- verifying process-group isolation
            assert len(children) == 1
            child = children[0]
            worker_pgid = os.getpgid(child.pid)
            assert worker_pgid == child.pid, "start_new_session=True must make the worker its own group leader"
            assert worker_pgid != os.getpgid(os.getpid()), "worker must not share the supervisor's process group"
        finally:
            raw.close()
