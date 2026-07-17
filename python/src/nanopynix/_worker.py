"""Subprocess worker — Nix execution over gRPC (multiprocessing pipe or stdio).

Threading model
  Two threads with a clear boundary:

  * **Event loop thread** — gRPC handlers, H2 transport, WorkerBackchannel,
    log-relay task, primop-dispatcher task.
  * **Evaluator thread** — dedicated ``ThreadPoolExecutor(max_workers=1)``
    for the thread-confined EvalState and its Values.
  * **Store threads** — a bounded executor for concurrent Store operations.

  The event loop never blocks on Nix — handlers dispatch via
  ``executor.run()`` and the loop stays free for log relaying, primop RPC
  dispatch, and other gRPC calls.  ``HandleRegistry`` is locked for
  cross-thread access.  ``LogCollector`` is inherently thread-safe
  (``janus.Queue``).

Spawned by ``Session._WorkerClient``.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import importlib
import json
import os
import signal
import sys
import time
import traceback
from typing import TYPE_CHECKING, Any, cast

from grpclib_transports.multiprocessing import serve_multiprocessing_endpoint
from grpclib_transports.protocol import DEFAULT_TUNING
from grpclib_transports.stdio import serve_stdio
from nanopynix_proto.nix.common import LogEvent, LogLevel
from nanopynix_proto.nix.worker import (
    CloseStoreRequest,
    CloseStoreResponse,
    GetVerbosityRequest,
    GetVerbosityResponse,
    InitRequest,
    InitResponse,
    OpenStoreRequest,
    OpenStoreResponse,
    SetVerbosityRequest,
    SetVerbosityResponse,
    ShutdownRequest,
    ShutdownResponse,
    SubscribeLogsRequest,
    WorkerServiceBase,
)

import nanopynix_expr
import nanopynix_util
from nanopynix._grpc_util import wrap_service_handlers
from nanopynix._handle_registry import HandleRegistry
from nanopynix._local import LocalRuntime
from nanopynix._process_title import set_process_title, set_worker_title
from nanopynix._worker_eval import EvalServiceHandler, close_eval_state
from nanopynix._worker_nix import NixThreadExecutor
from nanopynix._worker_primop import ThreadedRpcPrimopBridge
from nanopynix._worker_primop import (
    rpc_primop_callback_factory as rpc_primop_callback_factory,  # type: ignore[reportPrivateUsage] -- internal module, required for primop callback factory
)
from nanopynix._worker_store import StoreServiceHandler
from nanopynix.logging import LogCollector
from nanopynix.models import PrimOpSpec

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from grpclib._typing import (
        IServable,  # type: ignore[reportPrivateUsage] -- private import required by grpclib protobuf service binding
    )
    from grpclib_transports import WorkerBackchannel

# Re-export for the multiprocessing runner in _pool.py
__all__ = ["main", "run_worker", "worker_service_factory"]

# Keep handler slots for long-lived streams plus bounded concurrent Store RPCs.
_WORKER_MAX_CONCURRENCY = 32
_STORE_WORKERS = 4

# ── Primop registration ──────────────────────────────────────────────


def _import_callable(import_path: str) -> Callable[..., Any]:
    module_name, sep, attr_path = import_path.partition(":")
    if not sep or not module_name or not attr_path:
        raise ValueError(f"invalid primop import path: {import_path!r}")
    value: Any = importlib.import_module(module_name)
    for attr in attr_path.split("."):
        value = getattr(value, attr)
    if not callable(value):
        raise TypeError(f"primop import path is not callable: {import_path!r}")
    return value


def _register_primops(
    raw_specs: list[dict[str, Any]],
    rpc_bridge: Any = None,
) -> None:
    for raw in raw_specs:
        spec = PrimOpSpec.from_dict(raw)
        if spec.rpc:
            if rpc_bridge is None:
                raise RuntimeError(f"RPC primop {spec.name!r} registered without backchannel")
            callback = rpc_primop_callback_factory(rpc_bridge, spec.name, spec.arity)
        else:
            callback = _import_callable(spec.import_path)
        nanopynix_expr.register_primop(
            spec.name,
            spec.arity,
            spec.args,
            spec.doc,
            callback,
        )


def _install_worker_diagnostics(collector: LogCollector) -> None:
    def _dump_worker_diagnostics(signum: int, _frame: Any) -> None:
        timestamp = time.monotonic()
        print(  # noqa: T201 -- SIGUSR1 signal handler; logging framework may be unsafe to call from signal context
            f"\n=== nanopynix worker diagnostics signal={signum} monotonic={timestamp:.6f} ===",
            file=sys.stderr,
            flush=True,
        )
        print(f"log_collector={collector.stats()}", file=sys.stderr, flush=True)  # noqa: T201 -- same signal-handler reason
        print("python_threads:", file=sys.stderr, flush=True)  # noqa: T201 -- same signal-handler reason
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        print("=== end nanopynix worker diagnostics ===", file=sys.stderr, flush=True)  # noqa: T201 -- same signal-handler reason

    signal.signal(signal.SIGUSR1, _dump_worker_diagnostics)


# ── Worker state ─────────────────────────────────────────────────────


class WorkerState:
    """Shared mutable state held by all three service handlers.

    Thread confinement:
      * ``eval_state`` — evaluator thread only
      * ``collector`` — both threads (already thread-safe via ``janus.Queue``)
      * ``log_task`` — unused compatibility slot; parent consumes SubscribeLogs
      * ``handles`` — both threads (locked ``HandleRegistry``)
      * ``executor`` — event loop only (owns the evaluator thread)
      * ``store_executor`` — event loop only (owns bounded Store threads)
      * ``rpc_bridge`` — Nix thread reads, event loop writes (internal locking)
    """

    def __init__(self) -> None:
        self.eval_state: Any = None
        self.collector: LogCollector | None = None
        self.log_task: asyncio.Task[None] | None = None
        self.handles: HandleRegistry = HandleRegistry()
        self.runtime = LocalRuntime()
        self.executor: NixThreadExecutor | None = None
        self.store_executor: NixThreadExecutor | None = None
        self.owns_executor = True
        self.owns_store_executor = True
        self.rpc_bridge: ThreadedRpcPrimopBridge | None = None
        self.eval_store_handle: int | None = None
        self.nix_path: list[str] = []
        self.worker_subname: str = "worker"
        self.named_store_uris: dict[int, str] = {}


# ── WorkerService handler ────────────────────────────────────────────


@wrap_service_handlers
class WorkerServiceHandler(WorkerServiceBase):
    """Lifecycle handler: init, subscribe-logs, shutdown."""

    def __init__(self, state: WorkerState) -> None:
        self._state: WorkerState = state

    async def init(self, message: InitRequest) -> InitResponse:
        """Bootstrap Nix without splitting its global state across threads."""
        try:
            settings = dict(message.settings)
            # These environment variables must be in place before libstore
            # reads configuration. They are process state, not Nix state.
            if message.nix_conf is not None:
                os.environ["NIX_USER_CONF_FILES"] = message.nix_conf
            if settings:
                os.environ["NIX_CONFIG"] = "\n".join(f"{k} = {v}" for k, v in settings.items())

            assert self._state.executor is not None  # set by worker_service_factory before init
            await self._state.executor.run(self._init_nix, message, settings)

            return InitResponse(status="ok")

        except Exception:
            traceback.print_exc(file=sys.stderr)
            # Re-raise so the gRPC framework propagates the error.
            raise

    def _init_nix(self, message: InitRequest, settings: dict[str, str]) -> None:
        """Run every Nix C++ initialization operation on the Nix thread."""
        for feature in message.experimental_features:
            nanopynix_util.enable_experimental_feature(feature)
        self._state.runtime.initialize(
            settings=settings,
            load_config=message.load_config,
            verbosity=int(LogLevel.NOTICE) if message.verbosity is None else int(message.verbosity),
            pure_eval=message.pure_eval,
            restrict_eval=message.restrict_eval,
            allowed_uris=message.allowed_uris,
        )
        self._state.nix_path = list(message.nix_path)

        primops_raw = [
            {
                "name": p.name,
                "arity": p.arity,
                "args": list(p.args),
                "doc": p.doc,
                "import_path": p.import_path,
                "rpc": p.rpc,
            }
            for p in message.primops
        ]
        assert self._state.rpc_bridge is not None  # set by worker_service_factory before init
        _register_primops(primops_raw, rpc_bridge=self._state.rpc_bridge)

    async def open_store(self, message: OpenStoreRequest) -> OpenStoreResponse:
        assert self._state.store_executor is not None  # set by worker_service_factory before init
        store_handle, uri, store_dir = await self._state.store_executor.run(self._open_store, message.uri)
        return OpenStoreResponse(
            store_handle=store_handle,
            uri=uri,
            store_dir=store_dir,
        )

    def _open_store(self, uri: str) -> tuple[int, str, str]:
        store = self._state.runtime.open_store(uri)
        handle = self._state.handles.allocate(store, "store")
        store_uri = store.get_uri()
        self._state.named_store_uris[handle] = store_uri
        self._update_store_title()
        return handle, store_uri, store.get_store_dir()

    async def close_store(self, message: CloseStoreRequest) -> CloseStoreResponse:
        assert self._state.executor is not None  # set by worker_service_factory before init
        # A forced close may have to destroy the thread-confined EvalState, so
        # retain the evaluator lane for this lifecycle operation.
        await self._state.executor.run(self._close_store, message.store_handle, message.force)
        return CloseStoreResponse()

    def _close_store(self, store_handle: int, force: bool = False) -> None:
        if self._state.eval_store_handle == store_handle:
            if not force:
                raise RuntimeError("cannot close a store while its EvalState is open; call CloseEval first")
            close_eval_state(self._state)
        self._state.handles.release(store_handle)
        if self._state.named_store_uris.pop(store_handle, None) is not None:
            self._update_store_title()

    async def get_verbosity(self, message: GetVerbosityRequest) -> GetVerbosityResponse:
        """Return the worker's current Nix logger verbosity."""
        del message
        assert self._state.executor is not None  # set by worker_service_factory before init
        verbosity = await self._state.executor.run(self._state.runtime.get_verbosity)
        return GetVerbosityResponse(verbosity=LogLevel(verbosity))

    async def set_verbosity(self, message: SetVerbosityRequest) -> SetVerbosityResponse:
        """Update Nix logger verbosity on the Nix thread."""
        assert self._state.executor is not None  # set by worker_service_factory before init
        verbosity = await self._state.executor.run(self._state.runtime.set_verbosity, int(message.verbosity))
        return SetVerbosityResponse(verbosity=LogLevel(verbosity))

    def _update_store_title(self) -> None:
        subname = " ".join(self._state.named_store_uris.values()) or self._state.worker_subname
        set_process_title(subname, project_name="nanopynix")

    async def subscribe_logs(self, message: SubscribeLogsRequest) -> AsyncIterator[LogEvent]:
        """Server-streaming RPC — yield log events as they arrive."""
        collector = self._state.collector
        if collector is None:
            return

        try:
            async for event in collector.stream():
                if event is None:
                    break
                req_id, action, *args = event  # type: ignore[reportUnknownVariableType] -- collector.stream() yields Any, destructuring on unknown items
                yield LogEvent(
                    request_id=req_id,
                    action=action,
                    args_json=json.dumps(list(args), default=str),
                )
        except asyncio.CancelledError:
            pass

    async def shutdown(self, message: ShutdownRequest) -> ShutdownResponse:
        """Acknowledge shutdown request.

        The actual process exit happens when the transport / pipe closes.
        """
        collector = self._state.collector
        if collector is not None:
            collector.close()
        if self._state.rpc_bridge is not None:
            self._state.rpc_bridge.stop()
        if self._state.executor is not None and self._state.owns_executor:
            self._state.executor.shutdown(wait=False)
        if self._state.store_executor is not None and self._state.owns_store_executor:
            self._state.store_executor.shutdown(wait=False)
        return ShutdownResponse()


# ── Factory ──────────────────────────────────────────────────────────


def worker_service_factory(
    backchannel: WorkerBackchannel | None = None,
    *,
    executor: NixThreadExecutor | None = None,
    store_executor: NixThreadExecutor | None = None,
) -> list[IServable]:
    """Create service handlers with a shared WorkerState.

    Must be called *inside* the worker process (before Nix init so that
    the logger is installed early).

    Sets up:

    * ``LogCollector`` + log-relay task (event loop thread)
    * evaluator ``NixThreadExecutor`` (dedicated single-worker thread)
    * Store ``NixThreadExecutor`` (bounded, concurrent Store work)
    * ``ThreadedRpcPrimopBridge`` (Nix→event-loop primop dispatch)
    """
    collector = LogCollector()
    nanopynix_util.install_logger(collector.callback)
    with contextlib.suppress(RuntimeError, ValueError):
        _install_worker_diagnostics(collector)

    state = WorkerState()
    state.worker_subname = set_worker_title()
    state.collector = collector

    state.executor = NixThreadExecutor() if executor is None else executor
    state.owns_executor = executor is None
    state.store_executor = (
        NixThreadExecutor(max_workers=_STORE_WORKERS, thread_name_prefix="nix-store")
        if store_executor is None
        else store_executor
    )
    state.owns_store_executor = store_executor is None

    if backchannel is not None:
        loop = asyncio.get_running_loop()
        bridge = ThreadedRpcPrimopBridge(backchannel, loop)
        bridge.start()
        state.rpc_bridge = bridge

    return cast(
        "list[IServable]",
        [
            WorkerServiceHandler(state),
            StoreServiceHandler(state),
            EvalServiceHandler(state),
        ],
    )


# ── Async runner (for grpclib-transports multiprocessing mode) ───────


async def run_worker(
    endpoint: Any,
    tuning: Any = None,
    max_concurrency: int | None = _WORKER_MAX_CONCURRENCY,
) -> None:
    """Serve gRPC over a multiprocessing pipe endpoint.

    Called by the forkserver child process via ``_run_multiprocessing_worker``
    in grpclib_transports.
    """

    tuning = tuning or DEFAULT_TUNING
    handlers = worker_service_factory()
    await serve_multiprocessing_endpoint(
        endpoint,
        handlers,
        tuning=tuning,
        max_concurrency=max_concurrency,
    )

    # Cleanup after transport closes
    worker_state: WorkerState = cast("WorkerServiceHandler", handlers[0])._state  # type: ignore[reportPrivateUsage, reportUnknownVariableType, reportUnknownMemberType] -- private attr access, cascade from Any
    collector = worker_state.collector  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- cascade from WorkerState Any attributes
    if collector is not None:
        collector.close()  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes
    log_task = worker_state.log_task  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- cascade from WorkerState Any attributes
    if log_task is not None:
        log_task.cancel()  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes
        with contextlib.suppress(asyncio.CancelledError):
            await log_task
    if worker_state.rpc_bridge is not None:  # type: ignore[reportUnknownVariableType] -- cascade from WorkerState Any attributes
        worker_state.rpc_bridge.stop()  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes
    if worker_state.executor is not None:  # type: ignore[reportUnknownVariableType] -- cascade from WorkerState Any attributes
        worker_state.executor.shutdown(wait=True)  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes


# ── Stdio entry point (console_script / ``python -m nanopynix._worker``) ──


def main() -> None:
    """Stdio entry point — serves gRPC over stdin/stdout.

    This is the ``nanopynix-worker`` console_script target and also the
    fallback for ``sys.executable -m nanopynix._worker``.
    """
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_stdio_main())


async def _stdio_main() -> None:
    handlers = worker_service_factory()
    await serve_stdio(handlers, max_concurrency=_WORKER_MAX_CONCURRENCY)

    # Cleanup after transport closes
    worker_state: WorkerState = cast("WorkerServiceHandler", handlers[0])._state  # type: ignore[reportPrivateUsage, reportUnknownVariableType, reportUnknownMemberType] -- private attr access, cascade from Any
    collector = worker_state.collector  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- cascade from WorkerState Any attributes
    if collector is not None:
        collector.close()  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes
    log_task = worker_state.log_task  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- cascade from WorkerState Any attributes
    if log_task is not None:
        log_task.cancel()  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes
        with contextlib.suppress(asyncio.CancelledError):
            await log_task
    if worker_state.rpc_bridge is not None:  # type: ignore[reportUnknownVariableType] -- cascade from WorkerState Any attributes
        worker_state.rpc_bridge.stop()  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes
    if worker_state.executor is not None:  # type: ignore[reportUnknownVariableType] -- cascade from WorkerState Any attributes
        worker_state.executor.shutdown(wait=True)  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes


if __name__ == "__main__":
    main()
