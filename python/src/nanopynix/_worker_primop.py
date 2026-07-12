"""Worker-side RPC primop bridge — synchronously calls the manager via backchannel.

A single ``_RpcPrimopBridge`` instance lives in ``WorkerState``.
Its ``call()`` method MUST be synchronous because it is invoked from
the C++ ``py_primop_bridge`` during Nix evaluation.

Why ``nest_asyncio`` (nested event loop)?
  The C++ primop bridge runs on the asyncio event loop thread (same
  thread that handles gRPC).  When Nix evaluates a primop we hold the
  GIL and the event loop is blocked — we can't ``await`` without
  re-entering the event loop.  ``nest_asyncio.apply()`` patches
  ``loop.run_until_complete()`` to allow nested execution on a running
  loop; the I/O in ``select()`` temporarily releases the GIL, which
  is safe because re-acquisition happens on the same thread.

Alternatives considered:
  - Separate thread + event loop: requires a thread-safe transport
    (grpclib clients are not thread-safe) or a second H2 connection.
  - C++ promise/future: the event loop thread is blocked in C++, so
    no Python callback can run on it until the C++ function returns —
    we'd need a second thread anyway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanopynix_proto.nix.common import NullValue, ScalarValue

if TYPE_CHECKING:
    from grpclib_transports import WorkerBackchannel

_CALL_ROUTE = "/nix.manager.ManagerPrimopService/Call"


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


def _scalar_value_to_python(sv: ScalarValue) -> Any:
    from betterproto2 import which_one_of

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


class _RpcPrimopBridge:
    def __init__(self, backchannel: WorkerBackchannel) -> None:
        self._backchannel = backchannel

    def call(self, name: str, args: list[Any]) -> Any:
        from nanopynix_proto.nix.manager import CallPrimopRequest, CallPrimopResponse

        loop = __import__("asyncio").get_running_loop()

        async def _do_call() -> Any:
            request = CallPrimopRequest(
                name=name,
                args=[_python_to_scalar_value(a) for a in args],
            )
            response: CallPrimopResponse = await self._backchannel.call_unary(
                _CALL_ROUTE, request, CallPrimopResponse
            )
            if response.value is None:
                return None
            return _scalar_value_to_python(response.value)

        return loop.run_until_complete(_do_call())


def rpc_primop_callback_factory(bridge: _RpcPrimopBridge, name: str, arity: int):
    """Return a synchronous callable suitable for ``register_primop()``.

    The worker-side primop registration calls this factory for each
    ``PrimOpSpec`` where ``rpc=True``.  The returned callable forwards
    every invocation to the manager via the gRPC backchannel.
    """

    def _callback(*args: Any) -> Any:
        if len(args) != arity:
            raise TypeError(
                f"primop {name!r} expects {arity} arguments, got {len(args)}"
            )
        return bridge.call(name, list(args))

    return _callback
