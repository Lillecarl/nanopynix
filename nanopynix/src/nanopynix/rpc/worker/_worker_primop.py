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
from typing import TYPE_CHECKING, Any

import anyio
import anyio.from_thread
from nanopynix_bindings import util as nanopynix_util
from nanopynix_proto.nix.manager import CallPrimopRequest, CallPrimopResponse

from nanopynix._core._codec import deep_value_to_python, python_to_deep_value
from nanopynix._typechecking import BEARTYPING
from nanopynix._wire import CALL_ROUTE
from nanopynix.rpc._primop_wire import reraise_if_primop_failure

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable

    from grpclib_transports import WorkerBackchannel

_RPC_TIMEOUT = 300.0


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
    task -- see AGENTS.md's documented exception to the anyio-primitives
    convention for the same reasoning.
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
            args=[python_to_deep_value(a) for a in args],
            request_id=nanopynix_util.get_logger_request_id(),
        )
        with anyio.fail_after(_RPC_TIMEOUT):
            response: CallPrimopResponse = await self._backchannel.call_unary(
                CALL_ROUTE,
                request,
                CallPrimopResponse,
            )
        return deep_value_to_python(response.value)

    # ── called from the Nix thread ──────────────────────────────────

    def call(self, name: str, args: list[Any]) -> Any:
        portal = self._portal
        if portal is None:
            raise RuntimeError("primop bridge is not running")
        try:
            return portal.call(self._invoke_async, name, args)
        except TimeoutError as exc:
            raise TimeoutError(f"manager primop {name!r} timed out after {_RPC_TIMEOUT:.0f}s") from exc
        except Exception as exc:
            # A marked failure is the caller's own primop raising, and is
            # re-raised as a PrimopError so that the C++ bridge renders it the
            # way it renders a worker-side primop. Anything else is the
            # backchannel itself failing and propagates as itself.
            reraise_if_primop_failure(exc)
            raise


def rpc_primop_callback_factory(bridge: ThreadedRpcPrimopBridge, name: str, arity: int) -> Callable[..., Any]:
    """Return a synchronous callable suitable for ``register_primop()``.

    The worker-side primop registration calls this factory for each
    ``PrimOpSpec`` where ``rpc=True``.
    """

    def _callback(*args: Any) -> Any:
        if len(args) != arity:
            raise TypeError(f"primop {name!r} expects {arity} arguments, got {len(args)}")
        return bridge.call(name, list(args))

    return _callback
