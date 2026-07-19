"""L3 lifecycle coverage for the native-daemon control worker."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from grpclib_transports import inproc_worker_with_backchannel
from nanopynix_proto.nix.daemon import (
    DaemonServiceStub,
    GetDaemonStatusRequest,
    StartDaemonRequest,
    StopDaemonRequest,
    SubscribeDaemonLogsRequest,
)

from nanopynix.rpc.daemon._worker import daemon_service_factory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nanopynix_proto.nix.common import LogEvent


async def _next_log_event(stream: AsyncIterator[LogEvent]) -> LogEvent:
    return await anext(stream)


@pytest.mark.anyio
async def test_daemon_worker_controls_native_listener(tmp_path: Path) -> None:
    socket_path = tmp_path / "daemon.sock"

    async with inproc_worker_with_backchannel(daemon_service_factory, []) as channel:
        daemon = DaemonServiceStub(channel)
        log_stream = daemon.subscribe_logs(SubscribeDaemonLogsRequest())
        finalized = asyncio.create_task(_next_log_event(log_stream))
        await asyncio.sleep(0)
        before = await daemon.get_status(GetDaemonStatusRequest(request_id=1))
        assert not before.running
        first_finalized = await finalized
        assert first_finalized.request_id == 1
        assert first_finalized.request_finalized is not None

        started = await daemon.start(
            StartDaemonRequest(
                request_id=2,
                socket_path=str(socket_path),
                store_uri=f"local?root={tmp_path / 'store'}",
                load_config=False,
            )
        )
        assert started.socket_path == str(socket_path)
        running = await daemon.get_status(GetDaemonStatusRequest(request_id=3))
        assert running.running
        assert running.socket_path == str(socket_path)

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

        await daemon.stop(StopDaemonRequest(request_id=4))
        after = await daemon.get_status(GetDaemonStatusRequest(request_id=5))
        assert not after.running

    assert not socket_path.exists()
