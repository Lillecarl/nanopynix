"""Manager-side services exposed to the worker over the transport backchannel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import betterproto2
import grpclib
from betterproto2 import grpclib as betterproto2_grpclib
from grpclib.const import Cardinality, Handler, Status
from nanopynix_proto.nix.common import DeepAttrs, DeepList, DeepValue, LogEvent, NullValue, ScalarValue
from nanopynix_proto.nix.manager import CallPrimopRequest, CallPrimopResponse

from nanopynix.models import CALL_ROUTE

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

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


def _scalar_value_to_python(sv: ScalarValue | None) -> Any:  # noqa: PLR0911 tracked complexity/arg-count debt, see TODO.md
    if sv is None:
        return None
    kind = betterproto2.which_one_of(sv, "kind")[0]
    if kind == "string_value":
        return sv.string_value
    if kind == "int_value":
        return sv.int_value
    if kind == "float_value":
        return sv.float_value
    if kind == "bool_value":
        return sv.bool_value
    if kind == "null_value":
        return None
    return None


def _python_to_scalar_value(value: Any) -> ScalarValue:
    if value is None:
        return ScalarValue(null_value=NullValue())
    if isinstance(value, bool):
        return ScalarValue(bool_value=value)
    if isinstance(value, int):
        return ScalarValue(int_value=value)
    if isinstance(value, float):
        return ScalarValue(float_value=value)
    if isinstance(value, str):
        return ScalarValue(string_value=value)
    raise TypeError(f"unsupported RPC primop value type: {type(value)}")


def _python_to_deep_value(value: Any) -> DeepValue:
    """Encode a Python value (JSON-compatible: scalar/list/dict, arbitrarily
    nested) as the wire ``DeepValue`` primop RPC args/results carry."""
    if isinstance(value, dict):
        value_dict = cast("dict[str, Any]", value)
        return DeepValue(attrs=DeepAttrs(entries={k: _python_to_deep_value(v) for k, v in value_dict.items()}))
    if isinstance(value, list):
        value_list = cast("list[Any]", value)
        return DeepValue(list=DeepList(items=[_python_to_deep_value(v) for v in value_list]))
    return DeepValue(scalar=_python_to_scalar_value(value))


def _deep_value_to_python(dv: DeepValue | None) -> Any:
    if dv is None:
        return None
    if dv.scalar is not None:
        return _scalar_value_to_python(dv.scalar)
    if dv.list is not None:
        return [_deep_value_to_python(item) for item in dv.list.items]
    if dv.attrs is not None:
        return {k: _deep_value_to_python(v) for k, v in dv.attrs.entries.items()}
    if dv.remote_value is not None:
        raise TypeError("remote value handles are not supported for primop RPC args/results")
    return None


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

        args = [_deep_value_to_python(a) for a in request.args]
        try:
            result = func(*args)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as exc:
            raise grpclib.GRPCError(Status.INTERNAL, str(exc)) from exc

        return CallPrimopResponse(value=_python_to_deep_value(result))
