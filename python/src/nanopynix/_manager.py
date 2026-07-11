"""Manager-side services exposed to the worker over the transport backchannel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import betterproto2
import grpclib
from betterproto2 import grpclib as betterproto2_grpclib
from grpclib.const import Cardinality, Handler, Status

from nanopynix_proto.nix.common import LogEvent

if TYPE_CHECKING:
    from grpclib.server import Stream

_LOG_ROUTE = "/nix.manager.ManagerService/Log"


@dataclass(eq=False, repr=False)
class LogAck(betterproto2.Message):
    ok: bool = betterproto2.field(1, betterproto2.TYPE_BOOL)


class ManagerServiceBase(betterproto2_grpclib.ServiceBase):
    async def log(self, message: LogEvent) -> LogAck:
        raise grpclib.GRPCError(Status.UNIMPLEMENTED)

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
            )
        }


class ManagerServiceHandler(ManagerServiceBase):
    def __init__(self, log_callback: Any) -> None:
        self._log_callback = log_callback

    async def log(self, message: LogEvent) -> LogAck:
        self._log_callback(message)
        return LogAck(ok=True)
