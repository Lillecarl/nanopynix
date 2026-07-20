"""Thread-safe primop bridge — Nix thread → event loop → manager.

When a manager-side primop fires during Nix evaluation, the C++
``py_primop_bridge`` calls a Python callback synchronously on the
Nix thread.  That callback must make a gRPC backchannel call to the
manager, but the backchannel lives on the event loop thread.

This bridge uses an ``anyio.from_thread.BlockingPortal`` to run the
backchannel call on the event loop and block the Nix thread until it
completes — ``BlockingPortal`` is explicitly designed for arbitrary
foreign threads, not just ones anyio itself spawned.

No ``nest_asyncio`` needed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import anyio
import anyio.from_thread
from betterproto2 import which_one_of
from nanopynix_bindings import util as nanopynix_util
from nanopynix_proto.nix.common import DeepAttrs, DeepList, DeepValue, NullValue, ScalarValue
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
    raise TypeError(f"unsupported RPC primop value type: {type(value)}")


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


class ThreadedRpcPrimopBridge:
    """Bridges synchronous primop calls on the Nix thread to async
    backchannel RPCs on the event loop thread.

    ``start()``/``stop()`` are called from ``WorkerServiceHandler.init``/
    ``shutdown``, which -- like separate gRPC calls generally -- may each run
    as a different asyncio task. anyio's ``BlockingPortal`` owns a
    ``CancelScope``/``TaskGroup`` internally, which must be entered and
    exited by the same task, so its whole lifetime lives inside one
    dedicated background task (``_run``); ``start()``/``stop()`` merely
    signal that task via ``anyio.Event``, which -- unlike ``CancelScope``/
    ``TaskGroup`` -- has no task affinity and is safe to set/wait from any
    task. Same pattern as ``DaemonSupervisor`` in ``rpc/daemon/_supervisor.py``.
    """

    def __init__(self, backchannel: WorkerBackchannel) -> None:
        self._backchannel = backchannel
        self._portal: anyio.from_thread.BlockingPortal | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._stop_event: anyio.Event | None = None

    async def start(self) -> None:
        if self._run_task is not None:
            raise RuntimeError("primop bridge is already running")
        self._stop_event = anyio.Event()
        ready = anyio.Event()
        self._run_task = asyncio.create_task(self._run(ready), name="nix-primop-bridge")
        await ready.wait()

    async def stop(self) -> None:
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        run_task = self._run_task
        self._run_task = None
        if run_task is not None:
            await run_task
        self._portal = None

    async def _run(self, ready: anyio.Event) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            raise RuntimeError("primop bridge is not running")
        async with anyio.from_thread.BlockingPortal() as portal:
            self._portal = portal
            ready.set()
            await stop_event.wait()

    async def _invoke_async(self, name: str, args: list[Any]) -> Any:
        request = CallPrimopRequest(
            name=name,
            args=[_python_to_deep_value(a) for a in args],
            request_id=nanopynix_util.get_logger_request_id(),
        )
        with anyio.fail_after(_RPC_TIMEOUT):
            response: CallPrimopResponse = await self._backchannel.call_unary(
                _CALL_ROUTE, request, CallPrimopResponse
            )
        return _deep_value_to_python(response.value)

    # ── called from the Nix thread ──────────────────────────────────

    def call(self, name: str, args: list[Any]) -> Any:
        portal = self._portal
        if portal is None:
            raise RuntimeError("primop bridge is not running")
        try:
            return portal.call(self._invoke_async, name, args)
        except TimeoutError as exc:
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
