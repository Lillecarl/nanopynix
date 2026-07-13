"""Subprocess worker — Nix execution over gRPC (multiprocessing pipe or stdio).

Threading model
  Two threads with a clear boundary:

  * **Event loop thread** — gRPC handlers, H2 transport, WorkerBackchannel,
    log-relay task, primop-dispatcher task.
  * **Nix thread** — dedicated ``ThreadPoolExecutor(max_workers=1)`` for
    all Nix C++ operations (EvalState, Store, flake locking, evaluation).

  The event loop never blocks on Nix — handlers dispatch via
  ``executor.run()`` and the loop stays free for log relaying, primop RPC
  dispatch, and other gRPC calls.  ``HandleRegistry`` is locked for
  cross-thread access.  ``LogCollector`` is inherently thread-safe
  (``janus.Queue``).

Spawned by ``Session._WorkerManager``.
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

from nanopynix_proto.nix.common import LogEvent, LogLevel
from nanopynix_proto.nix.worker import (
    CloseStoreRequest,
    CloseStoreResponse,
    InitRequest,
    InitResponse,
    OpenStoreRequest,
    OpenStoreResponse,
    ShutdownRequest,
    ShutdownResponse,
    SubscribeLogsRequest,
    WorkerServiceBase,
)

import nanopynix_expr
import nanopynix_store
import nanopynix_util
from nanopynix._grpc_util import wrap_service_handlers
from nanopynix._handle_registry import HandleRegistry
from nanopynix._worker_eval import EvalServiceHandler
from nanopynix._worker_store import StoreServiceHandler
from nanopynix.logging import LogCollector
from nanopynix.models import PrimOpSpec

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from grpclib._typing import IServable
    from grpclib_transports import WorkerBackchannel

# Re-export for the multiprocessing runner in _pool.py
__all__ = ["main", "run_worker", "worker_service_factory"]

# Keep handler slots for the long-lived primop backchannel, the long-lived
# SubscribeLogs stream, and the active manager->worker call. EvalSession still
# serializes Nix eval RPCs at the ReservedWorker boundary.
_WORKER_MAX_CONCURRENCY = 3

_DIAGNOSTIC_SETTINGS = (
    "http-connections",
    "max-jobs",
    "build-dir",
    "substitute",
    "substituters",
    "trusted-public-keys",
    "builders",
    "builders-use-substitutes",
    "sandbox",
    "system",
    "system-features",
)


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
            from nanopynix._worker_primop import rpc_primop_callback_factory as rpc_primop_callback_factory

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


def _effective_settings_for_log() -> dict[str, str | None]:
    return {name: nanopynix_util.get_setting(name) for name in _DIAGNOSTIC_SETTINGS}


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
      * ``eval_state`` — Nix thread only (``_worker_nix.NixThreadExecutor``)
      * ``collector`` — both threads (already thread-safe via ``janus.Queue``)
      * ``log_task`` — unused compatibility slot; parent consumes SubscribeLogs
      * ``handles`` — both threads (locked ``HandleRegistry``)
      * ``executor`` — event loop only (owns the Nix thread)
      * ``rpc_bridge`` — Nix thread reads, event loop writes (internal locking)
    """

    def __init__(self) -> None:
        self.eval_state: Any = None
        self.collector: LogCollector | None = None
        self.log_task: asyncio.Task[None] | None = None
        self.handles: HandleRegistry = HandleRegistry()
        self.executor: Any = None
        self.rpc_bridge: Any = None
        self.eval_store_handle: int | None = None
        self.nix_path: list[str] = []


# ── WorkerService handler ────────────────────────────────────────────


@wrap_service_handlers
class WorkerServiceHandler(WorkerServiceBase):
    """Lifecycle handler: init, subscribe-logs, shutdown."""

    def __init__(self, state: WorkerState) -> None:
        self._state = state

    async def init(self, message: InitRequest) -> InitResponse:
        """Bootstrap Nix: configure, init libstore/libexpr, open store."""
        try:
            settings = dict(message.settings)
            # Apply config file path before init
            if message.nix_conf is not None:
                os.environ["NIX_USER_CONF_FILES"] = message.nix_conf
            if settings:
                os.environ["NIX_CONFIG"] = "\n".join(f"{k} = {v}" for k, v in settings.items())

            for k, v in settings.items():
                nanopynix_util.set_setting(k, v)
            for f in message.experimental_features:
                nanopynix_util.enable_experimental_feature(f)

            nanopynix_util.init_libstore(load_config=False)
            nanopynix_util.set_verbosity(int(message.verbosity if message.verbosity is not None else LogLevel.NOTICE))
            nanopynix_expr.init_libexpr()

            if message.pure_eval is not None:
                nanopynix_expr._set_pure_eval(message.pure_eval)
            if message.restrict_eval is not None:
                nanopynix_expr._set_restrict_eval(message.restrict_eval)
            if message.allowed_uris:
                nanopynix_expr._set_allowed_uris(message.allowed_uris)
            self._state.nix_path = list(message.nix_path)
            if self._state.collector is not None:
                self._state.collector.callback(
                    0,
                    "msg",
                    int(LogLevel.DEBUG),
                    "worker effective settings "
                    f"settings={_effective_settings_for_log()} nix_path={self._state.nix_path}",
                )

            # Convert proto PrimOpSpec list to the raw-dict format
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
            _register_primops(primops_raw, rpc_bridge=self._state.rpc_bridge)

            return InitResponse(status="ok")

        except Exception:
            traceback.print_exc(file=sys.stderr)
            # Re-raise so the gRPC framework propagates the error.
            raise

    async def open_store(self, message: OpenStoreRequest) -> OpenStoreResponse:
        store_handle, uri, store_dir = await self._state.executor.run(self._open_store, message.uri)
        return OpenStoreResponse(
            store_handle=store_handle,
            uri=uri,
            store_dir=store_dir,
        )

    def _open_store(self, uri: str) -> tuple[int, str, str]:
        store = nanopynix_store.open_store() if uri == "auto" else nanopynix_store.open_store(uri)
        handle = self._state.handles.allocate(store, "store")
        if self._state.collector is not None:
            self._state.collector.callback(
                0,
                "msg",
                int(LogLevel.DEBUG),
                f"worker open_store handle={handle} uri={store.get_uri()} dirs={store.get_store_dirs()}",
            )
        return handle, store.get_uri(), store.get_store_dir()

    async def close_store(self, message: CloseStoreRequest) -> CloseStoreResponse:
        await self._state.executor.run(self._state.handles.release, message.store_handle)
        return CloseStoreResponse()

    async def subscribe_logs(self, message: SubscribeLogsRequest) -> AsyncIterator[LogEvent]:
        """Server-streaming RPC — yield log events as they arrive."""
        collector = self._state.collector
        if collector is None:
            return

        try:
            async for event in collector.stream():
                if event is None:
                    break
                req_id, action, *args = event
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
        if self._state.executor is not None:
            self._state.executor.shutdown(wait=False)
        return ShutdownResponse()


# ── Factory ──────────────────────────────────────────────────────────


def worker_service_factory(backchannel: WorkerBackchannel | None = None) -> list[IServable]:
    """Create service handlers with a shared WorkerState.

    Must be called *inside* the worker process (before Nix init so that
    the logger is installed early).

    Sets up:

    * ``LogCollector`` + log-relay task (event loop thread)
    * ``NixThreadExecutor`` (dedicated single-worker thread)
    * ``ThreadedRpcPrimopBridge`` (Nix→event-loop primop dispatch)
    """
    collector = LogCollector()
    nanopynix_util.install_logger(collector.callback)
    with contextlib.suppress(RuntimeError, ValueError):
        _install_worker_diagnostics(collector)
    collector.callback(0, "msg", int(LogLevel.DEBUG), f"nanopynix worker pid={os.getpid()} diagnostics=SIGUSR1")

    state = WorkerState()
    state.collector = collector

    from nanopynix._worker_nix import NixThreadExecutor

    state.executor = NixThreadExecutor()

    if backchannel is not None:
        from nanopynix._worker_primop import ThreadedRpcPrimopBridge

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
    from grpclib_transports.multiprocessing import serve_multiprocessing_endpoint
    from grpclib_transports.protocol import DEFAULT_TUNING

    tuning = tuning or DEFAULT_TUNING
    handlers = worker_service_factory()
    await serve_multiprocessing_endpoint(
        endpoint,
        handlers,
        tuning=tuning,
        max_concurrency=max_concurrency,
    )

    # Cleanup after transport closes
    worker_handler = handlers[0]
    worker_state = cast("WorkerServiceHandler", worker_handler)._state
    collector = worker_state.collector
    if collector is not None:
        collector.close()
    log_task = worker_state.log_task
    if log_task is not None:
        log_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await log_task
    if worker_state.rpc_bridge is not None:
        worker_state.rpc_bridge.stop()
    if worker_state.executor is not None:
        worker_state.executor.shutdown(wait=True)


# ── Stdio entry point (console_script / ``python -m nanopynix._worker``) ──


def main() -> None:
    """Stdio entry point — serves gRPC over stdin/stdout.

    This is the ``nanopynix-worker`` console_script target and also the
    fallback for ``sys.executable -m nanopynix._worker``.
    """
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_stdio_main())


async def _stdio_main() -> None:
    from grpclib_transports.stdio import serve_stdio

    handlers = worker_service_factory()
    await serve_stdio(handlers, max_concurrency=_WORKER_MAX_CONCURRENCY)

    # Cleanup after transport closes
    worker_handler = handlers[0]
    worker_state = cast("WorkerServiceHandler", worker_handler)._state
    collector = worker_state.collector
    if collector is not None:
        collector.close()
    log_task = worker_state.log_task
    if log_task is not None:
        log_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await log_task
    if worker_state.rpc_bridge is not None:
        worker_state.rpc_bridge.stop()
    if worker_state.executor is not None:
        worker_state.executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
