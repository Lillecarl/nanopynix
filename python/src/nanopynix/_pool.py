# ruff: noqa: ASYNC109
"""Multiprocessing worker — Nix execution backend via gRPC over pipe transport.

A single forkserver subprocess runs an independent Nix process with its own
Store, logger, and globals.  Communication is gRPC over a multiprocessing pipe
pair via grpclib-transports.

Only one call is in-flight at a time — the worker is single-threaded.
``_WorkerManager.reserve()`` holds the lock for the duration of an
``EvalSession``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from grpclib.exceptions import GRPCError, StreamTerminatedError

from nanopynix._worker import worker_service_factory
from nanopynix.exceptions import from_response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nanopynix_proto.nix.eval import EvalServiceStub
    from nanopynix_proto.nix.store import StoreServiceStub
    from nanopynix_proto.nix.worker import WorkerServiceStub

    from nanopynix.models import PrimOpSpec

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
_RPC_TIMEOUT = 300.0
# The grpclib-transports backchannel is a long-lived bidi stream on the same H2
# connection. Keep one handler slot for that stream and one for the active
# manager->worker call. EvalSession still serializes Nix eval RPCs at the
# ReservedWorker boundary.
_WORKER_MAX_CONCURRENCY = 2


# ════════════════════════════════════════════════════════════════════
# Exceptions
# ════════════════════════════════════════════════════════════════════


class WorkerDiedError(RuntimeError):
    """Raised when the subprocess worker dies unexpectedly."""


class WorkerBusyError(RuntimeError):
    """Raised when the single worker is already handling another operation."""


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
        raise from_response("Unknown", exc.message or str(exc))
    except (StreamTerminatedError, ConnectionError) as exc:
        raise WorkerDiedError(str(exc)) from exc


# ════════════════════════════════════════════════════════════════════
# _LogBus + _Subscription
# ════════════════════════════════════════════════════════════════════


class _LogBus:
    """Subscriber list for worker log events.

    Zero subscribers → events are discarded (no buffering, no overhead).
    Callbacks are called synchronously from the read loop — keep them fast.
    """

    def __init__(self) -> None:
        self._subscribers: list = []

    def subscribe(self, callback) -> _Subscription:
        self._subscribers.append(callback)
        return _Subscription(self, callback)

    def _unsubscribe(self, sub: _Subscription) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(sub._callback)

    def emit(self, event: object) -> None:
        if not self._subscribers:
            return
        for cb in self._subscribers:
            try:
                cb(event)
            except Exception:
                logger.exception("worker log subscriber failed")


class _Subscription:
    """Handle returned by ``_LogBus.subscribe()``."""

    __slots__ = ("_bus", "_callback")

    def __init__(self, bus: _LogBus, callback) -> None:
        self._bus = bus
        self._callback = callback

    def unsubscribe(self) -> None:
        self._bus._unsubscribe(self)


# ════════════════════════════════════════════════════════════════════
# ReservedWorker — public token for an exclusive worker lease
# ════════════════════════════════════════════════════════════════════


class ReservedWorker:
    """Exclusive lease on the session worker, obtained via ``_WorkerManager.reserve()``.

    Provides access to ``_eval_stub`` and ``_store_stub`` for direct gRPC
    calls.  Calls made through ``call()`` are serialized because the worker's
    Nix state is single-threaded even when the underlying transport can
    multiplex gRPC streams.
    """

    __slots__ = ("_manager", "_released", "_rpc_lock")

    def __init__(self, manager: _WorkerManager) -> None:
        self._manager = manager
        self._released = False
        self._rpc_lock = asyncio.Lock()

    @property
    def _eval_stub(self):
        return self._manager._eval_stub

    @property
    def _store_stub(self):
        return self._manager._store_stub

    async def call(self, coro: Any) -> Any:
        """Await one reserved-worker RPC with gRPC error conversion."""
        if self._released:
            coro.close()
            raise WorkerDiedError("reserved worker has been released")
        async with self._rpc_lock:
            return await _grpc_call(coro)

    async def release(self) -> None:
        """Return the worker to the manager.  Idempotent — safe to call twice."""
        if not self._released:
            self._released = True
            self._manager._release()


# ════════════════════════════════════════════════════════════════════
# _WorkerManager — single-worker lifecycle
# ════════════════════════════════════════════════════════════════════


class _WorkerManager:
    """Manages a single multiprocessing worker with an independent Nix Store.

    Provides:
    - ``reserve()`` — exclusive worker lease (used by EvalSession).
    - ``subscribe()`` / ``log_stream()`` — log event access.
    - Direct access to ``_store_stub`` and ``_eval_stub`` for gRPC calls.
    """

    def __init__(
        self,
        *,
        store_uri: str = "auto",
        nix_conf: str | None = "/etc/nix/nix.conf",
        settings: dict[str, str] | None = None,
        experimental_features: list[str] | None = None,
        primops: list[PrimOpSpec] | None = None,
        worker_oom_score_adj: int | None = None,
        reserved_worker_oom_score_adj: int | None = None,
    ) -> None:
        self._store_uri = store_uri
        self._nix_conf = nix_conf
        self._settings = settings or {}
        self._features = experimental_features or []
        self._primops = primops or []
        # OOM score adjustment is not yet supported with multiprocessing transport
        # (no direct access to child PID).  Params kept for API compat.
        self._channel = None
        self._worker_service_stub: WorkerServiceStub | None = None
        self._store_service_stub: StoreServiceStub | None = None
        self._eval_service_stub: EvalServiceStub | None = None
        self._available: asyncio.Lock = asyncio.Lock()
        self._log_bus: _LogBus = _LogBus()
        self._stack: contextlib.AsyncExitStack | None = None

    # ── lifecycle ──────────────────────────────────────────────────

    async def open(self) -> None:
        """Spawn the worker via multiprocessing forkserver and initialise Nix."""
        from grpclib_transports.multiprocessing import multiprocessing_worker_with_backchannel
        from nanopynix._manager import ManagerServiceHandler
        from nanopynix_proto.nix.eval import EvalServiceStub
        from nanopynix_proto.nix.store import StoreServiceStub
        from nanopynix_proto.nix.worker import InitRequest, WorkerServiceStub

        self._stack = contextlib.AsyncExitStack()
        self._channel = await self._stack.enter_async_context(
            multiprocessing_worker_with_backchannel(
                worker_service_factory,
                [ManagerServiceHandler(self._log_bus.emit)],
                preload=["nanopynix._worker"],
                max_concurrency=_WORKER_MAX_CONCURRENCY,
            )
        )
        self._worker_service_stub = WorkerServiceStub(self._channel)
        self._store_service_stub = StoreServiceStub(self._channel)
        self._eval_service_stub = EvalServiceStub(self._channel)

        # Convert PrimOpSpec to proto
        from nanopynix_proto.nix.common import PrimOpSpec as PrimOpSpecPB

        proto_primops = [
            PrimOpSpecPB(
                name=p.name,
                arity=p.arity,
                args=list(p.args),
                doc=p.doc,
                import_path=p.import_path,
            )
            for p in self._primops
        ]

        # Initialize Nix in the worker
        init_response = await self._worker_stub.init(
            InitRequest(
                store_uri=self._store_uri,
                nix_conf=self._nix_conf,
                settings=self._settings,
                experimental_features=self._features,
                primops=proto_primops,
            ),
            timeout=_RPC_TIMEOUT,
        )
        if init_response.status != "ok":
            raise RuntimeError(f"Worker init failed: {init_response.status}")

    async def close(self) -> None:
        """Shut down the worker."""
        try:
            if self._worker_service_stub is not None:
                from nanopynix_proto.nix.worker import ShutdownRequest

                try:
                    await self._worker_stub.shutdown(ShutdownRequest(), timeout=5.0)
                except (GRPCError, StreamTerminatedError, ConnectionError, asyncio.TimeoutError, asyncio.CancelledError):
                    logger.debug("worker shutdown failed (expected during teardown)", exc_info=True)
        finally:
            if self._stack is not None:
                await self._stack.aclose()
                self._stack = None

        self._log_bus.emit(None)

    # ── worker lock ────────────────────────────────────────────────

    async def reserve(self, timeout: float | None = None) -> ReservedWorker:
        """Acquire an exclusive worker lease for an EvalSession."""
        if self._channel is None:
            raise WorkerDiedError("Worker not started")
        if self._available.locked():
            if timeout is None:
                raise WorkerBusyError("worker is busy")
            try:
                await asyncio.wait_for(self._available.acquire(), timeout=timeout)
            except TimeoutError as exc:
                raise WorkerBusyError(f"worker is busy after waiting {timeout}s") from exc
        else:
            await self._available.acquire()
        return ReservedWorker(self)

    def _release(self) -> None:
        """Release the worker lock (called by ReservedWorker.release())."""
        self._available.release()

    async def call(self, coro: Any, *, timeout: float | None = None) -> Any:
        """Acquire the worker lock and await a gRPC coroutine.

        Raises ``WorkerBusyError`` if the worker is reserved by an
        ``EvalSession`` and no timeout is given.
        """
        if self._available.locked():
            if timeout is None:
                coro.close()
                raise WorkerBusyError("worker is busy")
            try:
                await asyncio.wait_for(self._available.acquire(), timeout=timeout)
            except TimeoutError as exc:
                coro.close()
                raise WorkerBusyError(f"worker is busy after waiting {timeout}s") from exc
            try:
                return await coro
            finally:
                self._available.release()
        async with self._available:
            return await coro

    # ── log access ─────────────────────────────────────────────────

    async def log_stream(self) -> AsyncIterator[object]:
        """Async iterator over log events."""
        q: asyncio.Queue = asyncio.Queue()

        def _on_event(event: object) -> None:
            q.put_nowait(event)

        sub = self._log_bus.subscribe(_on_event)
        try:
            while True:
                event = await q.get()
                if event is None:
                    break
                yield event
        finally:
            sub.unsubscribe()

    def subscribe(self, callback) -> _Subscription:
        """Subscribe a callback to all log events.

        Callback receives ``LogEvent`` proto messages from the worker.
        Returns a ``_Subscription`` — call ``.unsubscribe()`` to stop.
        """
        return self._log_bus.subscribe(callback)

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
