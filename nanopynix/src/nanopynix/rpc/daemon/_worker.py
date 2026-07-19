"""L3 gRPC control worker for isolated native-Nix daemon listeners."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from grpclib_transports.multiprocessing import serve_multiprocessing_endpoint
from grpclib_transports.protocol import DEFAULT_TUNING
from nanopynix_proto.nix.common import LogEvent, NixLogEvent, RequestFinalized
from nanopynix_proto.nix.daemon import (
    DaemonServiceBase,
    GetDaemonStatusRequest,
    GetDaemonStatusResponse,
    StartDaemonRequest,
    StartDaemonResponse,
    StopDaemonRequest,
    StopDaemonResponse,
    SubscribeDaemonLogsRequest,
)

from nanopynix.logging import LogCollector
from nanopynix.rpc.daemon._config import DaemonConfig
from nanopynix.rpc.daemon._supervisor import DaemonSupervisor
from nanopynix.rpc.worker._grpc_util import wrap_service_handlers

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from grpclib._typing import (
        IServable,  # type: ignore[reportPrivateUsage] -- required by generated grpclib server bindings
    )


class DaemonWorkerState:
    """State confined to one L3 daemon-control worker process."""

    def __init__(self) -> None:
        self.collector = LogCollector()
        self.supervisor: DaemonSupervisor | None = None

    async def run_request(self, request_id: int, operation: Callable[[], Awaitable[Any]]) -> Any:
        """Finalize every control RPC after its listener operation completes."""
        try:
            if request_id <= 0:
                raise ValueError("request_id must be positive")
            return await operation()
        finally:
            self.collector.request_finalized(request_id)

    async def close(self) -> None:
        """Close the listener before releasing its operation-boundary stream."""
        supervisor = self.supervisor
        self.supervisor = None
        if supervisor is not None:
            await supervisor.close()
        self.collector.close()


@wrap_service_handlers
class DaemonServiceHandler(DaemonServiceBase):
    """Own exactly one native daemon listener within a dedicated L3 worker."""

    def __init__(self, state: DaemonWorkerState) -> None:
        self._state = state

    async def start(self, message: StartDaemonRequest) -> StartDaemonResponse:
        async def operation() -> StartDaemonResponse:
            if self._state.supervisor is not None:
                raise RuntimeError("native daemon is already running")
            config = DaemonConfig(
                store_uri=message.store_uri,
                settings=dict(message.settings),
                load_config=message.load_config,
                nix_conf=message.nix_conf,
            )
            supervisor = DaemonSupervisor(Path(message.socket_path), config)
            await supervisor.start()
            self._state.supervisor = supervisor
            return StartDaemonResponse(socket_path=str(supervisor.socket_path))

        return cast("StartDaemonResponse", await self._state.run_request(message.request_id, operation))

    async def stop(self, message: StopDaemonRequest) -> StopDaemonResponse:
        async def operation() -> StopDaemonResponse:
            supervisor = self._state.supervisor
            if supervisor is not None:
                await supervisor.close()
                self._state.supervisor = None
            return StopDaemonResponse()

        return cast("StopDaemonResponse", await self._state.run_request(message.request_id, operation))

    async def get_status(self, message: GetDaemonStatusRequest) -> GetDaemonStatusResponse:
        async def operation() -> GetDaemonStatusResponse:
            supervisor = self._state.supervisor
            if supervisor is None or not supervisor.running:
                return GetDaemonStatusResponse(running=False)
            return GetDaemonStatusResponse(running=True, socket_path=str(supervisor.socket_path))

        return cast("GetDaemonStatusResponse", await self._state.run_request(message.request_id, operation))

    async def subscribe_logs(self, message: SubscribeDaemonLogsRequest) -> AsyncIterator[LogEvent]:
        del message
        async for event in self._state.collector.stream():
            kind, request_id, *payload = event
            if kind == "finalized":
                yield LogEvent(request_id=request_id, request_finalized=RequestFinalized())
            elif kind == "nix":
                action, *args = payload
                yield LogEvent(request_id=request_id, nix_log=NixLogEvent(action=action, args_json=json.dumps(args)))


def daemon_service_factory(*_args: object) -> list[IServable]:
    """Construct control-plane handlers without initialising Nix in the supervisor."""
    return [DaemonServiceHandler(DaemonWorkerState())]


async def run_daemon_worker(
    endpoint: Any,
    tuning: Any = None,
    max_concurrency: int | None = 32,
) -> None:
    """Serve daemon lifecycle gRPC until its L3 transport closes."""
    state = DaemonWorkerState()
    handlers: list[IServable] = [DaemonServiceHandler(state)]
    try:
        await serve_multiprocessing_endpoint(
            endpoint,
            handlers,
            tuning=DEFAULT_TUNING if tuning is None else tuning,
            max_concurrency=max_concurrency,
        )
    finally:
        with contextlib.suppress(asyncio.CancelledError):
            await state.close()
