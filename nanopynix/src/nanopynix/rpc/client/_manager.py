"""Manager-side services exposed to the worker over the transport backchannel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import betterproto2
import grpclib
from betterproto2 import grpclib as betterproto2_grpclib
from grpclib.const import Cardinality, Handler, Status
from nanopynix_proto.nix.common import LogEvent
from nanopynix_proto.nix.manager import CallPrimopRequest, CallPrimopResponse

from nanopynix._core._codec import deep_value_to_python, python_to_deep_value
from nanopynix._typechecking import BEARTYPING, no_runtime_type_check
from nanopynix._wire import CALL_ROUTE

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Mapping

    from grpclib.server import Stream

_LOG_ROUTE = "/nix.manager.ManagerService/Log"


@dataclass(eq=False, repr=False)
class LogAck(betterproto2.Message):
    ok: bool = betterproto2.field(1, betterproto2.TYPE_BOOL)


class ManagerServiceBase(betterproto2_grpclib.ServiceBase):
    async def log(self, message: LogEvent) -> LogAck:
        raise grpclib.GRPCError(Status.UNIMPLEMENTED)

    @no_runtime_type_check  # `stream` is duck-typed against grpclib.server.Stream's protocol, not an instance of it, when dispatched in-process via grpclib_transports.control._UnaryServerStream
    async def __rpc_log(self, stream: Stream[LogEvent, LogAck]) -> None:
        request = await stream.recv_message()
        if request is None:
            raise grpclib.GRPCError(Status.INVALID_ARGUMENT, "missing log event")
        response = await self.log(request)
        await stream.send_message(response)

    def __mapping__(self) -> dict[str, Handler]:
        return {
            _LOG_ROUTE: Handler(
                self.__rpc_log,
                Cardinality.UNARY_UNARY,
                LogEvent,
                LogAck,
            ),
        }


class ManagerServiceHandler(ManagerServiceBase):
    def __init__(self, log_callback: Any) -> None:
        self._log_callback = log_callback

    async def log(self, message: LogEvent) -> LogAck:
        self._log_callback(message)
        return LogAck(ok=True)


# ── ManagerPrimopService ────────────────────────────────────────────────


class ManagerPrimopServiceBase(betterproto2_grpclib.ServiceBase):
    async def call(self, request: CallPrimopRequest) -> CallPrimopResponse:
        raise grpclib.GRPCError(Status.UNIMPLEMENTED)

    @no_runtime_type_check  # see __rpc_log above
    async def __rpc_call(self, stream: Stream[CallPrimopRequest, CallPrimopResponse]) -> None:
        request = await stream.recv_message()
        if request is None:
            raise grpclib.GRPCError(Status.INVALID_ARGUMENT, "missing call request")
        response = await self.call(request)
        await stream.send_message(response)

    def __mapping__(self) -> dict[str, Handler]:
        return {
            CALL_ROUTE: Handler(
                self.__rpc_call,
                Cardinality.UNARY_UNARY,
                CallPrimopRequest,
                CallPrimopResponse,
            ),
        }


class ManagerPrimopServiceHandler(ManagerPrimopServiceBase):
    def __init__(self) -> None:
        self._registry: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, callback: Callable[..., Any]) -> None:
        self._registry[name] = callback

    def register_all(self, callables: Mapping[str, Callable[..., Any]]) -> None:
        for name, callback in callables.items():
            self._registry[name] = callback

    async def call(self, request: CallPrimopRequest) -> CallPrimopResponse:
        func = self._registry.get(request.name)
        if func is None:
            raise grpclib.GRPCError(Status.NOT_FOUND, f"primop {request.name!r} not registered")

        args = [deep_value_to_python(a) for a in request.args]
        try:
            result = func(*args)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as exc:
            raise grpclib.GRPCError(Status.INTERNAL, str(exc)) from exc

        return CallPrimopResponse(value=python_to_deep_value(result))
