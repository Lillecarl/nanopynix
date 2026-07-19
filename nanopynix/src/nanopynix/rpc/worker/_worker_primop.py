"""Thread-safe primop bridge — Nix thread → event loop → manager.

When a manager-side primop fires during Nix evaluation, the C++
``py_primop_bridge`` calls a Python callback synchronously on the
Nix thread.  That callback must make a gRPC backchannel call to the
manager, but the backchannel lives on the event loop thread.

This bridge uses ``call_soon_threadsafe`` to enqueue the request
on the event loop, then blocks on a ``concurrent.futures.Future``
whose ``result()`` releases the GIL — allowing the event loop to
process the backchannel call and fulfil the future.

No ``nest_asyncio`` needed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import TYPE_CHECKING, Any

from betterproto2 import which_one_of
from nanopynix_bindings import util as nanopynix_util
from nanopynix_proto.nix.common import NullValue, ScalarValue
from nanopynix_proto.nix.manager import CallPrimopRequest, CallPrimopResponse

if TYPE_CHECKING:
    from grpclib_transports import WorkerBackchannel

_CALL_ROUTE = "/nix.manager.ManagerPrimopService/Call"
_RPC_TIMEOUT = 300.0


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
    raise TypeError(f"unsupported RPC primop arg type: {type(value)}")


def _scalar_value_to_python(sv: ScalarValue | None) -> Any:
    if sv is None:
        return None
    kind = which_one_of(sv, "kind")[0]
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


class ThreadedRpcPrimopBridge:
    """Bridges synchronous primop calls on the Nix thread to async
    backchannel RPCs on the event loop thread."""

    def __init__(self, backchannel: WorkerBackchannel, loop: asyncio.AbstractEventLoop) -> None:
        self._backchannel = backchannel
        self._loop = loop
        self._queue: asyncio.Queue[tuple[str, list[Any], concurrent.futures.Future[Any]]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._dispatch_loop(), name="nix-primop-dispatcher")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _dispatch_loop(self) -> None:
        while True:
            name, args, future = await self._queue.get()
            try:
                request = CallPrimopRequest(
                    name=name,
                    args=[_python_to_scalar_value(a) for a in args],
                    request_id=nanopynix_util.get_logger_request_id(),
                )
                response: CallPrimopResponse = await self._backchannel.call_unary(
                    _CALL_ROUTE, request, CallPrimopResponse
                )
                future.set_result(_scalar_value_to_python(response.value))
            except Exception as exc:
                future.set_exception(exc)

    # ── called from the Nix thread ──────────────────────────────────

    def call(self, name: str, args: list[Any]) -> Any:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (name, args, future))
        try:
            return future.result(timeout=_RPC_TIMEOUT)
        except concurrent.futures.TimeoutError as exc:
            raise TimeoutError(f"manager primop {name!r} timed out after {_RPC_TIMEOUT:.0f}s") from exc


def rpc_primop_callback_factory(bridge: ThreadedRpcPrimopBridge, name: str, arity: int):
    """Return a synchronous callable suitable for ``register_primop()``.

    The worker-side primop registration calls this factory for each
    ``PrimOpSpec`` where ``rpc=True``.
    """

    def _callback(*args: Any) -> Any:
        if len(args) != arity:
            raise TypeError(f"primop {name!r} expects {arity} arguments, got {len(args)}")
        return bridge.call(name, list(args))

    return _callback
