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

Spawned by ``Session``'s ``rpc.client._pool.WorkerClient``.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import json
import os
import signal
import sys
import time
import traceback
from typing import TYPE_CHECKING, Any, cast

import anyio
import anyio.to_thread
from grpclib_transports.multiprocessing import serve_multiprocessing_endpoint
from grpclib_transports.protocol import DEFAULT_TUNING
from grpclib_transports.stdio import serve_stdio
from nanopynix_bindings import expr as nanopynix_expr
from nanopynix_bindings import util as nanopynix_util
from nanopynix_proto.nix.common import LogEvent, LogLevel, NixLogEvent, RequestFinalized
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

from nanopynix._core._local import LocalRuntime
from nanopynix._core._primops import import_primop_callable as _import_callable
from nanopynix._process_title import set_process_title, set_worker_title
from nanopynix._wire import (
    NIX_CONFIG_ENV,
    NIX_USER_CONF_FILES_ENV,
    WORKER_INIT_STATUS_OK,
    HandleKind,
)
from nanopynix.logging import LogCollector, LogStreamEventKind
from nanopynix.models import PrimOpSpec
from nanopynix.rpc._status_details import NIX_STATUS_DETAILS_CODEC
from nanopynix.rpc.worker._grpc_util import wrap_service_handlers
from nanopynix.rpc.worker._handle_registry import HandleRegistry
from nanopynix.rpc.worker._worker_eval import EvalServiceHandler, close_eval_state, find_evals_by_store
from nanopynix.rpc.worker._worker_nix import NixThreadExecutor
from nanopynix.rpc.worker._worker_primop import ThreadedRpcPrimopBridge
from nanopynix.rpc.worker._worker_primop import (
    rpc_primop_callback_factory as rpc_primop_callback_factory,  # type: ignore[reportPrivateUsage] -- internal module, required for primop callback factory
)
from nanopynix.rpc.worker._worker_store import StoreServiceHandler
from nanopynix.settings import DEFAULT_WORKER_MAX_CONCURRENCY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from grpclib._typing import (
        IServable,  # type: ignore[reportPrivateUsage] -- private import required by grpclib protobuf service binding
    )
    from grpclib_transports import WorkerBackchannel

# Re-export for the multiprocessing runner in _pool.py
__all__ = ["main", "run_worker", "worker_service_factory"]

_STORE_WORKERS = 4

# ── Primop registration ──────────────────────────────────────────────


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
      * ``collector`` — both threads (already thread-safe via ``janus.Queue``)
      * ``log_task`` — unused compatibility slot; parent consumes SubscribeLogs
      * ``handles`` — both threads (locked ``HandleRegistry``); each open
        evaluator lives here as an ``EvalEntry`` (kind ``"eval"``), owning its
        own dedicated Nix thread — see ``_worker_eval.py``.
      * ``executor`` — event loop only. Process-global Nix operations only
        (``Init``, ``GetVerbosity``/``SetVerbosity``, the store-close
        bookkeeping in ``_close_store``) — evaluator work runs on each
        evaluator's own executor instead.
      * ``store_limiter`` — event loop only (bounds concurrent Store work
        dispatched via ``anyio.to_thread.run_sync``; unlike ``executor``, a
        ``CapacityLimiter`` has no thread-affinity requirement and no
        shutdown lifecycle of its own to own/release)
      * ``rpc_bridge`` — Nix thread reads, event loop writes (internal locking)
    """

    def __init__(self) -> None:
        self.collector: LogCollector | None = None
        self.log_task: asyncio.Task[None] | None = None
        self.handles: HandleRegistry = HandleRegistry()
        self.runtime = LocalRuntime()
        self.executor: NixThreadExecutor | None = None
        self.store_limiter: anyio.CapacityLimiter | None = None
        self.owns_executor = True
        self.rpc_bridge: ThreadedRpcPrimopBridge | None = None
        self.nix_path: list[str] = []
        self.worker_subname: str = "worker"
        self.named_store_uris: dict[int, str] = {}

    async def run_request(
        self,
        *,
        request_id: int,
        operation: Callable[..., Any],
        args: tuple[Any, ...] = (),
        executor: NixThreadExecutor | None = None,
        limiter: anyio.CapacityLimiter | None = None,
    ) -> Any:
        """Run a unary operation with its request-local logger context installed.

        Exactly one of ``executor`` (a dedicated-thread ``NixThreadExecutor``,
        for evaluator-affine work) or ``limiter`` (an
        ``anyio.CapacityLimiter`` bounding ``anyio.to_thread.run_sync`` calls,
        for Store work) must be given -- these are two distinct dispatch
        mechanisms, not variants of one shared interface.
        """
        collector = self.collector
        if request_id <= 0:
            if collector is not None:
                collector.request_finalized(request_id)
            raise ValueError("request_id must be positive")
        if (executor is None) == (limiter is None):
            raise ValueError("run_request requires exactly one of executor or limiter")

        def _run() -> Any:
            previous = nanopynix_util.get_logger_request_id()
            nanopynix_util.set_logger_request_id(request_id)
            try:
                return operation(*args)
            finally:
                if collector is not None:
                    collector.request_finalized(request_id)
                nanopynix_util.set_logger_request_id(previous)

        if executor is not None:
            return await executor.run(_run)
        return await anyio.to_thread.run_sync(_run, limiter=limiter)

    def log(self, action: str, *args: object) -> None:
        """Emit a diagnostic in the current executor thread's request context."""
        if self.collector is not None:
            self.collector.callback(nanopynix_util.get_logger_request_id(), action, *args)


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
                os.environ[NIX_USER_CONF_FILES_ENV] = message.nix_conf
            if settings:
                rendered_settings = "\n".join(f"{key} = {value}" for key, value in settings.items())
                inherited_settings = os.environ.get(NIX_CONFIG_ENV)
                os.environ[NIX_CONFIG_ENV] = (
                    f"{inherited_settings}\n{rendered_settings}" if inherited_settings else rendered_settings
                )

            if self._state.executor is None:
                raise RuntimeError("worker executor is unavailable")  # noqa: TRY301 -- guard clause intentionally caught by except block which prints traceback and re-raises
            if self._state.rpc_bridge is not None:
                # worker_service_factory() must stay synchronous (it is
                # handed to grpclib_transports as a BackchannelServiceFactory
                # callback invoked without await), so the bridge's
                # BlockingPortal -- whose __aenter__ needs a running event
                # loop and an await -- starts here instead, on the first
                # async operation the worker performs. Init always precedes
                # OpenEval/primop registration, so this is ready before any
                # primop could actually fire.
                await self._state.rpc_bridge.start()
            await self._state.run_request(
                request_id=message.request_id,
                executor=self._state.executor,
                operation=self._init_nix,
                args=(message, settings),
            )

            return InitResponse(status=WORKER_INIT_STATUS_OK)

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
        if self._state.rpc_bridge is None:
            raise RuntimeError("worker rpc_bridge is unavailable")  # set by worker_service_factory before init
        _register_primops(primops_raw, rpc_bridge=self._state.rpc_bridge)

    async def open_store(self, message: OpenStoreRequest) -> OpenStoreResponse:
        if self._state.store_limiter is None:
            raise RuntimeError("worker store limiter is unavailable")
        store_handle, uri, store_dir = await self._state.run_request(
            request_id=message.request_id,
            limiter=self._state.store_limiter,
            operation=self._open_store,
            args=(message.uri,),
        )
        return OpenStoreResponse(
            store_handle=store_handle,
            uri=uri,
            store_dir=store_dir,
        )

    def _open_store(self, uri: str) -> tuple[int, str, str]:
        store = self._state.runtime.open_store(uri)
        handle = self._state.handles.allocate(store, HandleKind.STORE)
        store_uri = store.get_uri()
        self._state.named_store_uris[handle] = store_uri
        self._update_store_title()
        return handle, store_uri, store.get_store_dir()

    async def close_store(self, message: CloseStoreRequest) -> CloseStoreResponse:
        if self._state.executor is None:
            raise RuntimeError("worker executor is unavailable")
        # A forced close may have to destroy the thread-confined EvalState, so
        # retain the evaluator lane for this lifecycle operation.
        await self._state.run_request(
            request_id=message.request_id,
            executor=self._state.executor,
            operation=self._close_store,
            args=(message.store_handle, message.force),
        )
        return CloseStoreResponse()

    def _close_store(self, store_handle: int, force: bool = False) -> None:
        try:
            store = self._state.handles.get_typed(store_handle, HandleKind.STORE)
        except KeyError:
            # Store close is idempotent: a client or session teardown may
            # repeat a successful forced close.
            return
        eval_handles = find_evals_by_store(self._state, store_handle)
        if eval_handles:
            if not force:
                raise RuntimeError("cannot close a store while its EvalState is open; call CloseEval first")
            for eval_handle in eval_handles:
                close_eval_state(self._state, eval_handle)
        store.close()
        self._state.handles.release(store_handle)
        if self._state.named_store_uris.pop(store_handle, None) is not None:
            self._update_store_title()

    async def get_verbosity(self, message: GetVerbosityRequest) -> GetVerbosityResponse:
        """Return the worker's current Nix logger verbosity."""
        if self._state.executor is None:
            raise RuntimeError("worker executor is unavailable")
        verbosity = await self._state.run_request(
            request_id=message.request_id, executor=self._state.executor, operation=self._state.runtime.get_verbosity
        )
        return GetVerbosityResponse(verbosity=LogLevel(verbosity))

    async def set_verbosity(self, message: SetVerbosityRequest) -> SetVerbosityResponse:
        """Update Nix logger verbosity on the Nix thread."""
        if self._state.executor is None:
            raise RuntimeError("worker executor is unavailable")
        verbosity = await self._state.run_request(
            request_id=message.request_id,
            executor=self._state.executor,
            operation=self._state.runtime.set_verbosity,
            args=(int(message.verbosity),),
        )
        return SetVerbosityResponse(verbosity=LogLevel(verbosity))

    def _update_store_title(self) -> None:
        subname = " ".join(self._state.named_store_uris.values()) or self._state.worker_subname
        set_process_title(subname, project_name="nanopynix")

    async def subscribe_logs(self, message: SubscribeLogsRequest) -> AsyncIterator[LogEvent]:
        """Server-streaming RPC — yield log events as they arrive.

        This is the wire-encoding hop from ``LogCollector`` to protobuf, so it
        is deliberately not built on :class:`nanopynix.logging.CallbackBus`
        (the in-process pub-sub shared by ``inproc.Session`` and the client's
        ``WorkerClient``) — there is nothing to subscribe/unsubscribe here,
        only a single collector stream serialized onto a gRPC channel.
        """
        collector = self._state.collector
        if collector is None:
            return

        try:
            async for event in collector.stream():
                if event is None:
                    break
                kind, request_id, *payload = event
                if kind == LogStreamEventKind.NIX:
                    action, *args = payload
                    yield LogEvent(
                        request_id=request_id,
                        nix_log=NixLogEvent(action=action, args_json=json.dumps(args, default=str)),
                    )
                elif kind == LogStreamEventKind.FINALIZED:
                    yield LogEvent(request_id=request_id, request_finalized=RequestFinalized())
        except anyio.get_cancelled_exc_class():
            pass

    async def shutdown(self, message: ShutdownRequest) -> ShutdownResponse:
        """Acknowledge shutdown request.

        The actual process exit happens when the transport / pipe closes.
        """
        if self._state.executor is None:
            raise RuntimeError("worker executor is unavailable")
        await self._state.run_request(
            request_id=message.request_id, executor=self._state.executor, operation=lambda: None
        )
        # End the SubscribeLogs stream from this side, before the client stops
        # waiting on it. Without this the client's only way out is to cancel,
        # which resets the stream under a server handler that is still inside
        # its `async for` -- and grpclib logs that as "Failed to handle
        # cancellation" on the worker's stderr, which the parent inherits. The
        # events this drops were already being dropped by that cancellation.
        if self._state.collector is not None:
            await self._state.collector.asend_sentinel()
        if self._state.rpc_bridge is not None:
            await self._state.rpc_bridge.stop()
        if self._state.owns_executor:
            self._state.executor.shutdown(wait=False)
        return ShutdownResponse()


# ── Factory ──────────────────────────────────────────────────────────


def worker_service_factory(
    backchannel: WorkerBackchannel | None = None,
    *,
    executor: NixThreadExecutor | None = None,
    store_limiter: anyio.CapacityLimiter | None = None,
) -> list[IServable]:
    """Create service handlers with a shared WorkerState.

    Must be called *inside* the worker process (before Nix init so that
    the logger is installed early).

    Sets up:

    * ``LogCollector`` + log-relay task (event loop thread)
    * evaluator ``NixThreadExecutor`` (dedicated single-worker thread)
    * Store ``anyio.CapacityLimiter`` (bounds concurrent Store work on
      anyio's shared thread pool)
    * ``ThreadedRpcPrimopBridge`` (Nix→event-loop primop dispatch; its
      ``BlockingPortal`` is started later, from ``WorkerServiceHandler.init``
      -- this factory must stay synchronous to satisfy grpclib_transports'
      ``BackchannelServiceFactory`` contract)
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
    state.store_limiter = anyio.CapacityLimiter(_STORE_WORKERS) if store_limiter is None else store_limiter

    if backchannel is not None:
        state.rpc_bridge = ThreadedRpcPrimopBridge(backchannel)

    return cast(
        "list[IServable]",
        [
            WorkerServiceHandler(state),
            StoreServiceHandler(state),
            EvalServiceHandler(state),
        ],
    )


# ── Async runner (for grpclib-transports multiprocessing mode) ───────


async def _shutdown_worker(handlers: list[IServable]) -> None:
    """Tear down the shared `WorkerState` after a transport closes -- shared
    by both `run_worker` (multiprocessing) and `_stdio_main` (stdio), which
    otherwise hand-repeated this identically."""
    worker_state: WorkerState = cast("WorkerServiceHandler", handlers[0])._state  # type: ignore[reportPrivateUsage, reportUnknownVariableType, reportUnknownMemberType] -- private attr access, cascade from Any  # noqa: SLF001
    collector = worker_state.collector  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- cascade from WorkerState Any attributes
    if collector is not None:
        collector.close()  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes
    log_task = worker_state.log_task  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- cascade from WorkerState Any attributes
    if log_task is not None:
        log_task.cancel()  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes
        with contextlib.suppress(anyio.get_cancelled_exc_class()):
            await log_task
    if worker_state.rpc_bridge is not None:  # type: ignore[reportUnknownVariableType] -- cascade from WorkerState Any attributes
        await worker_state.rpc_bridge.stop()  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes
    if worker_state.executor is not None:  # type: ignore[reportUnknownVariableType] -- cascade from WorkerState Any attributes
        worker_state.executor.shutdown(wait=True)  # type: ignore[reportUnknownMemberType] -- cascade from WorkerState Any attributes


async def run_worker(
    endpoint: Any,
    tuning: Any = None,
    max_concurrency: int | None = DEFAULT_WORKER_MAX_CONCURRENCY,
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
        status_details_codec=NIX_STATUS_DETAILS_CODEC,
    )
    await _shutdown_worker(handlers)


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
    await serve_stdio(
        handlers,
        max_concurrency=DEFAULT_WORKER_MAX_CONCURRENCY,
        status_details_codec=NIX_STATUS_DETAILS_CODEC,
    )
    await _shutdown_worker(handlers)


if __name__ == "__main__":
    main()
