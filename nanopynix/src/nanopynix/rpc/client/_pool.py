"""Multiprocessing worker — Nix execution backend via gRPC over pipe transport.

A single forkserver subprocess runs an independent Nix process with its own
Store, logger, and globals.  Communication is gRPC over a multiprocessing pipe
pair via grpclib-transports.

Evaluator calls remain serial because an ``EvalState`` is thread-confined. Store
calls may be in flight concurrently: the worker dispatches them to its bounded
Store executor.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from grpclib.exceptions import GRPCError, StreamTerminatedError
from grpclib_transports.multiprocessing import multiprocessing_worker_with_backchannel
from nanopynix_proto.nix.common import PrimOpSpec as PrimOpSpecPB
from nanopynix_proto.nix.eval import EvalServiceStub
from nanopynix_proto.nix.store import StoreServiceStub
from nanopynix_proto.nix.worker import (
    GetVerbosityRequest,
    InitRequest,
    SetVerbosityRequest,
    ShutdownRequest,
    SubscribeLogsRequest,
    WorkerServiceStub,
)

from nanopynix.exceptions import from_response
from nanopynix.logging import BusSubscription, CallbackBus
from nanopynix.models import DEFAULT_STORE_URI, WORKER_INIT_STATUS_OK
from nanopynix.rpc.client._manager import ManagerPrimopServiceHandler
from nanopynix.rpc.worker._worker import (
    _WORKER_MAX_CONCURRENCY,  # type: ignore[reportPrivateUsage] -- cross-class access
    worker_service_factory,
)
from nanopynix.settings import DEFAULT_RPC_TIMEOUT_SECONDS, DEFAULT_SHUTDOWN_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from nanopynix_proto.nix.common import LogLevel

    from nanopynix.models import PrimOpSpec

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
_RPC_TIMEOUT = DEFAULT_RPC_TIMEOUT_SECONDS
_OOM_SCORE_ADJ_MIN = -1000
_OOM_SCORE_ADJ_MAX = 1000

_ACTIVE_LOG_CAPTURES: ContextVar[tuple[Any, ...]] = ContextVar("nanopynix_active_log_captures", default=())


# ════════════════════════════════════════════════════════════════════
# Exceptions
# ════════════════════════════════════════════════════════════════════


class WorkerDiedError(RuntimeError):
    """Raised when the subprocess worker dies unexpectedly."""


# ════════════════════════════════════════════════════════════════════
# gRPC error helper
# ════════════════════════════════════════════════════════════════════


async def _grpc_call(coro: Any) -> Any:
    """Execute a gRPC stub call, converting GRPCError to NixError.

    Usage::

        resp = await _grpc_call(stub.method(request, timeout=...))
    """
    try:
        return await coro
    except GRPCError as exc:
        raise from_response("Unknown", exc.message or str(exc)) from exc
    except (StreamTerminatedError, ConnectionError) as exc:
        raise WorkerDiedError(str(exc)) from exc


def _clamp_oom_score_adj(value: int) -> int:
    return min(_OOM_SCORE_ADJ_MAX, max(_OOM_SCORE_ADJ_MIN, value))


def _write_oom_score_adj(pid: int, value: int, *, proc_root: Path = Path("/proc")) -> None:
    path = proc_root / str(pid) / "oom_score_adj"
    path.write_text(f"{_clamp_oom_score_adj(value)}\n")


# ════════════════════════════════════════════════════════════════════
# Log dispatch — see nanopynix.logging.CallbackBus for why this is shared
# with inproc.Session and not with the worker's own subscribe_logs.
# ════════════════════════════════════════════════════════════════════
# _WorkerClient — single-worker lifecycle and operation dispatch
# ════════════════════════════════════════════════════════════════════


class _WorkerClient:  # pyright: ignore[reportUnusedClass] -- imported by the public Session façade
    """Own one multiprocessing worker and dispatch its RPC operations.

    Provides:
    - ``call()`` — worker RPC dispatch without cross-service serialization.
    - ``subscribe()`` / ``log_stream()`` — log event access.
    - Direct access to ``_store_stub`` and ``_eval_stub`` for gRPC calls.
    """

    def __init__(  # noqa: PLR0913 tracked complexity/arg-count debt, see TODO.md
        self,
        *,
        store_uri: str = DEFAULT_STORE_URI,
        nix_conf: Path | None = None,
        load_config: bool = True,
        settings: dict[str, str] | None = None,
        experimental_features: list[str] | None = None,
        verbosity: LogLevel | None = None,
        nix_path: Sequence[str] | None = None,
        primops: list[PrimOpSpec] | None = None,
        primop_callables: dict[str, Callable[..., Any]] | None = None,
        worker_oom_score_adj: int | None = None,
        rpc_timeout: float = _RPC_TIMEOUT,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self._store_uri = store_uri
        self._nix_conf = nix_conf
        self._load_config = load_config
        self._settings = settings or {}
        self._features = experimental_features or []
        self._verbosity = verbosity
        self._nix_path = list(nix_path) if nix_path else []
        self._primops = primops or []
        self._primop_callables = primop_callables or {}
        self._worker_oom_score_adj = worker_oom_score_adj
        self.rpc_timeout = rpc_timeout
        self._shutdown_timeout = shutdown_timeout
        self._worker_pid: int | None = None
        self._channel = None
        self._worker_service_stub: WorkerServiceStub | None = None
        self._store_service_stub: StoreServiceStub | None = None
        self._eval_service_stub: EvalServiceStub | None = None
        self._primop_handler: Any = None
        self._log_bus: CallbackBus = CallbackBus()
        self._log_task: asyncio.Task[None] | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._next_request_id = 1

    # ── lifecycle ──────────────────────────────────────────────────

    async def open(self) -> None:
        """Spawn the worker via multiprocessing forkserver and initialise Nix."""
        self._stack = contextlib.AsyncExitStack()

        self._primop_handler = ManagerPrimopServiceHandler()
        self._primop_handler.register_all(self._primop_callables)

        self._channel = await self._stack.enter_async_context(
            multiprocessing_worker_with_backchannel(
                worker_service_factory,
                [
                    self._primop_handler,
                ],
                on_process_start=self._on_worker_process_start,
                preload=["nanopynix.rpc.worker._worker"],
                max_concurrency=_WORKER_MAX_CONCURRENCY,
            ),
        )
        self._worker_service_stub = WorkerServiceStub(self._channel)
        self._store_service_stub = StoreServiceStub(self._channel)
        self._eval_service_stub = EvalServiceStub(self._channel)
        self._log_task = asyncio.create_task(self._forward_worker_logs(), name="nanopynix-worker-logs")

        proto_primops = [
            PrimOpSpecPB(
                name=p.name,
                arity=p.arity,
                args=list(p.args),
                doc=p.doc,
                import_path=p.import_path,
                rpc=p.rpc if hasattr(p, "rpc") else False,
            )
            for p in self._primops
        ]

        # Initialize Nix in the worker
        init_response = await self.invoke(
            self._worker_stub.init,
            InitRequest(
                store_uri=self._store_uri,
                nix_conf=str(self._nix_conf) if self._nix_conf is not None else None,
                load_config=self._load_config,
                settings=self._settings,
                experimental_features=self._features,
                primops=proto_primops,
                verbosity=self._verbosity,
                nix_path=self._nix_path,
            ),
            timeout=self.rpc_timeout,
        )
        if init_response.status != WORKER_INIT_STATUS_OK:
            raise RuntimeError(f"Worker init failed: {init_response.status}")

    async def close(self) -> None:
        """Shut down the worker."""
        try:
            if self._worker_service_stub is not None:
                try:
                    await self.invoke(self._worker_stub.shutdown, ShutdownRequest(), timeout=self._shutdown_timeout)
                except (
                    TimeoutError,
                    GRPCError,
                    StreamTerminatedError,
                    ConnectionError,
                    WorkerDiedError,
                    anyio.get_cancelled_exc_class(),
                ):
                    logger.debug("worker shutdown failed (expected during teardown)", exc_info=True)
        finally:
            if self._log_task is not None:
                self._log_task.cancel()
                with contextlib.suppress(anyio.get_cancelled_exc_class()):
                    await self._log_task
                self._log_task = None
            if self._stack is not None:
                await self._stack.aclose()
                self._stack = None

        self._log_bus.emit(None)

    # ── operation dispatch ─────────────────────────────────────────

    async def invoke(self, method: Callable[..., Any], request: Any, *, timeout: float) -> Any:  # noqa: ASYNC109 -- timeout passed to grpclib stub method which accepts a timeout parameter
        """Assign a worker-local operation ID and dispatch one unary RPC."""
        if self._channel is None:
            raise WorkerDiedError("Worker not started")
        request_id = self._next_request_id
        self._next_request_id += 1
        request.request_id = request_id
        for capture in _ACTIVE_LOG_CAPTURES.get():
            capture._register_request(request_id)  # type: ignore[reportPrivateUsage] -- capture registration is the dispatch contract  # noqa: SLF001
        return await _grpc_call(method(request, timeout=timeout))

    # ── log access ─────────────────────────────────────────────────

    async def log_stream(self) -> AsyncIterator[object]:
        """Async iterator over log events."""
        send_stream, receive_stream = anyio.create_memory_object_stream[object](max_buffer_size=math.inf)

        def _on_event(event: object) -> None:
            send_stream.send_nowait(event)

        sub = self._log_bus.subscribe(_on_event)
        try:
            async for event in receive_stream:
                if event is None:
                    break
                yield event
        finally:
            sub.unsubscribe()

    def subscribe(self, callback: Callable[..., None]) -> BusSubscription:
        """Subscribe a callback to all log events.

        Callback receives ``LogEvent`` proto messages from the worker.
        Returns a ``BusSubscription`` — call ``.unsubscribe()`` to stop.
        """
        return self._log_bus.subscribe(callback)

    async def _forward_worker_logs(self) -> None:
        try:
            async for event in self._worker_stub.subscribe_logs(SubscribeLogsRequest()):
                self._log_bus.emit(event)
        except anyio.get_cancelled_exc_class():
            raise
        except (GRPCError, StreamTerminatedError, ConnectionError):
            logger.debug("worker log stream ended", exc_info=True)

    def _on_worker_process_start(self, proc: Any) -> None:
        self._worker_pid = proc.pid
        self._set_worker_oom_score_adj(self._worker_oom_score_adj)

    def _set_worker_oom_score_adj(self, value: int | None) -> None:
        if value is None:
            return
        pid = self._worker_pid
        if pid is None:
            return
        try:
            _write_oom_score_adj(pid, value)
        except OSError:
            logger.debug("failed to set worker oom_score_adj", exc_info=True)

    async def get_verbosity(self) -> LogLevel:
        """Return the current worker-side Nix log verbosity."""
        response = await self.invoke(self._worker_stub.get_verbosity, GetVerbosityRequest(), timeout=self.rpc_timeout)
        self._verbosity = response.verbosity
        return response.verbosity

    async def set_verbosity(self, verbosity: LogLevel) -> LogLevel:
        """Set the worker-side Nix log verbosity."""
        response = await self.invoke(
            self._worker_stub.set_verbosity, SetVerbosityRequest(verbosity=verbosity), timeout=self.rpc_timeout
        )
        self._verbosity = response.verbosity
        return response.verbosity

    @property
    def _worker_stub(self) -> WorkerServiceStub:
        stub = self._worker_service_stub
        if stub is None:
            raise WorkerDiedError("Worker not started")
        return stub

    @property
    def _store_stub(self) -> StoreServiceStub:
        stub = self._store_service_stub
        if stub is None:
            raise WorkerDiedError("Worker not started")
        return stub

    @property
    def _eval_stub(self) -> EvalServiceStub:
        stub = self._eval_service_stub
        if stub is None:
            raise WorkerDiedError("Worker not started")
        return stub
