"""Thread-safe bridge from Nix's C++ logger to Python consumers.

The C++ ``PyLogger`` calls the callback from *any* thread after
``gil_scoped_acquire``, so the callback must be thread-safe.  We use a
``janus.Queue`` which provides both a synchronous (thread-safe) interface
for the worker subprocess and an async interface for the Nix manager client.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import threading
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import anyio
import janus
from nanopynix_proto.nix.common import LogEvent as LogEventProto

from nanopynix._typechecking import BEARTYPING
from nanopynix.models import LogEvent

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncIterator, Callable

    type LogCallback = Callable[..., None]

_logger = logging.getLogger(__name__)

ACTIVE_LOG_CAPTURES: ContextVar[tuple[LogCapture, ...]] = ContextVar("nanopynix_active_log_captures", default=())
"""Task-local stack of :class:`LogCapture` instances currently recording.

The dispatch contract both engines implement: whatever allocates an operation
id tags it into every capture active in the calling task, so the capture knows
which operations to wait for without the caller threading ids around. rpc does
it in ``WorkerClient.invoke()``; inproc does it where it allocates the id.

Task-local, so two concurrent captures in sibling tasks each see only their own
work -- which is why this is a ``ContextVar`` and not a field on either
session.
"""


class LogStreamEventKind(enum.StrEnum):
    """Discriminant tag for the tuples LogCollector's queue carries."""

    NIX = "nix"
    FINALIZED = "finalized"


class LogCollector:
    """Thread-safe collector for Nix log events.

    Pass ``collector.callback`` to ``nanopynix_util.install_logger()``.

    Usage::

        collector = LogCollector()
        nanopynix_util.install_logger(collector.callback)

        # Sync drain (worker subprocess):
        for event in collector.drain():
            ...

        # Async stream (Nix manager client):
        async for event in collector.stream():
            ...

        collector.stop()
    """

    def __init__(self, maxsize: int = 10_000) -> None:
        self._maxsize: int = maxsize
        self._queue: janus.Queue[Any] = janus.Queue(maxsize=maxsize)
        self._enqueued: int = 0
        self._stats_lock = threading.Lock()

    # ── callback (thread-safe, called from C++ on any GIL thread) ──

    def callback(self, req_id: int, action: str, *args: object) -> None:
        """Push an event onto the queue — thread-safe and lossless.

        If the manager falls behind, this deliberately backpressures the Nix
        logger callback instead of dropping events. The worker event loop drains
        the async side through ``SubscribeLogs``.
        """
        self._queue.sync_q.put((LogStreamEventKind.NIX, req_id, action, *args))
        with self._stats_lock:
            self._enqueued += 1

    def request_finalized(self, request_id: int) -> None:
        """Enqueue the typed operation boundary after its Nix work finishes."""
        self._queue.sync_q.put((LogStreamEventKind.FINALIZED, request_id))
        with self._stats_lock:
            self._enqueued += 1

    def stats(self) -> dict[str, int | bool]:
        """Return queue counters for worker signal diagnostics."""
        with self._stats_lock:
            enqueued = self._enqueued
        return {
            "maxsize": self._maxsize,
            "qsize": self._queue.sync_q.qsize(),
            "full": self._queue.sync_q.full(),
            "empty": self._queue.sync_q.empty(),
            "enqueued": enqueued,
        }

    # ── sync drain (for the worker subprocess) ─────────────────────

    def drain(self) -> list[Any]:
        """Return all currently pending events without blocking."""
        events: list[Any] = []
        try:
            while True:
                events.append(self._queue.sync_q.get_nowait())
        except janus.SyncQueueEmpty:
            pass
        return events

    # ── async stream (for the Nix manager client) ──────────────────

    async def stream(self) -> AsyncIterator[Any]:
        """Yield events as they arrive.

        Terminates either on a ``None`` sentinel or when the queue is
        closed via ``close()`` / ``aclose()``.
        """
        try:
            while True:
                item = await self._queue.async_q.get()
                if item is None:
                    break
                yield item
        except asyncio.queues.QueueShutDown:
            pass

    # ── shutdown ───────────────────────────────────────────────────

    def close(self) -> None:
        """Synchronous close — for the worker subprocess (no event loop)."""
        self._queue.close()

    async def aclose(self) -> None:
        """Async close — proper cleanup of janus internal tasks."""
        await self._queue.aclose()

    def send_sentinel(self) -> None:
        """Push a ``None`` sentinel to unblock ``stream()`` without closing."""
        self._queue.sync_q.put(None)

    async def asend_sentinel(self) -> None:
        """Async :meth:`send_sentinel`, for callers already on an event loop.

        The queue is bounded, so the synchronous variant can block; from the
        worker's shutdown handler that would stall the loop it is trying to
        wind down.
        """
        await self._queue.async_q.put(None)


class BusSubscription:
    """Handle returned by :meth:`CallbackBus.subscribe` — call ``.unsubscribe()`` to stop."""

    __slots__ = ("_bus", "_callback")

    def __init__(self, bus: CallbackBus, callback: LogCallback) -> None:
        self._bus = bus
        self._callback = callback

    def unsubscribe(self) -> None:
        self._bus._unsubscribe(self)  # type: ignore[reportPrivateUsage] -- required for cross-class callbacks  # noqa: SLF001


class CallbackBus:
    """Dispatch events synchronously to a list of subscribed callbacks.

    Shared by :class:`nanopynix.inproc.Session` (dispatching already-decoded
    log events directly, no wire hop) and the RPC client's ``WorkerClient``
    (dispatching events received over gRPC from the worker's own
    ``LogCollector``). The worker side (``rpc.worker._worker.subscribe_logs``)
    is deliberately not built on this class: it runs *inside* the worker
    process and must serialize events to protobuf over a streaming RPC, which
    is a wire-encoding step this in-process callback dispatch has no part of.

    Zero subscribers -> events are discarded (no buffering, no overhead). A
    subscriber that raises is logged and does not stop dispatch to the
    remaining subscribers.
    """

    def __init__(self) -> None:
        self._subscribers: list[LogCallback] = []

    def subscribe(self, callback: LogCallback) -> BusSubscription:
        self._subscribers.append(callback)
        return BusSubscription(self, callback)

    def _unsubscribe(self, sub: BusSubscription) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(sub._callback)  # type: ignore[reportPrivateUsage] -- required for cross-class callbacks  # noqa: SLF001

    def emit(self, event: object) -> None:
        if not self._subscribers:
            return
        for callback in tuple(self._subscribers):
            try:
                callback(event)
            except Exception:
                _logger.exception("log bus subscriber failed")


# `@runtime_checkable` because this Protocol annotates a parameter. Without it
# beartype cannot decorate `LogCapture.__init__` at all and skips the whole
# method, rather than checking it loosely. The check is `isinstance` against
# the member names below -- structural, which is the same guarantee the
# annotation already makes, and no stronger. This is the reference site: the
# other Protocols in this position point here.
@runtime_checkable
class LogEventBus(Protocol):
    """What a :class:`LogCapture` needs from whatever produces log events.

    Just ``subscribe``. inproc's ``Session`` and rpc's ``WorkerClient`` both
    satisfy it, which is the whole reason ``LogCapture`` can be one class: the
    events differ only in how they got here, and by the time they reach the bus
    they are the same :class:`~nanopynix.models.LogEvent` type either way --
    inproc emits the model directly, rpc's arrive as the proto it subclasses.
    """

    def subscribe(self, callback: LogCallback) -> BusSubscription: ...


class LogCapture:
    """Async context manager that records log events while active.

    Engine-independent: log capture is about nanopynix's own event bus, not
    about where Nix is running, so both engines expose the same object from
    ``session.capture_logs()``. It used to live in ``rpc.client.session`` and
    was rpc-only, which the signature ledger carried as
    "Session.capture_logs:rpc-only" -- an inproc caller had to subscribe to
    the bus and reimplement the filtering and the request bookkeeping by hand.

    Two things are recorded. :attr:`events` accumulates the Nix log events
    themselves. Separately, every operation dispatched inside the block is
    tagged via :data:`ACTIVE_LOG_CAPTURES`, so :meth:`wait` can block until
    each of them has finalized -- exiting the block does that automatically,
    which is what makes the captured list complete rather than merely
    whatever had arrived by then.
    """

    def __init__(self, bus: LogEventBus) -> None:
        self._bus = bus
        self._sub: BusSubscription | None = None
        self.events: list[LogEvent] = []
        self._request_ids: set[int] = set()
        self._finalized: set[int] = set()
        self._waiters: dict[int, anyio.Event] = {}
        self._active = False
        self._token: object | None = None

    @property
    def request_ids(self) -> frozenset[int]:
        """Snapshot of operation IDs started inside this capture's scope."""
        return frozenset(self._request_ids)

    def _register_request(self, request_id: int) -> None:
        """Tag `request_id` as started inside this capture's scope.

        Called only through :data:`ACTIVE_LOG_CAPTURES` by whichever engine is
        dispatching -- an internal contract, not part of ``LogCapture``'s
        public API (unlike ``events``/``request_ids``/``wait``/
        ``wait_for_request``, which callers use directly).
        """
        if self._active:
            self._request_ids.add(request_id)
            self._waiters.setdefault(request_id, anyio.Event())

    async def wait_for_request(self, request_id: int) -> None:
        if request_id not in self._request_ids:
            raise ValueError(f"request {request_id} is not registered with this log capture")
        if request_id in self._finalized:
            return
        await self._waiters[request_id].wait()

    async def wait(self) -> None:
        request_ids = tuple(self._request_ids)
        async with anyio.create_task_group() as tg:
            for request_id in request_ids:
                tg.start_soon(self.wait_for_request, request_id)

    def _append(self, raw: object) -> None:
        if not isinstance(raw, LogEventProto):
            return
        # inproc puts LogEvent (which *is* a LogEventProto subclass) on the bus
        # and rpc puts the bare proto; from_proto normalises both to the model
        # whose is_nix_log/is_request_finalized accessors this depends on.
        event = LogEvent.from_proto(raw)
        if event.is_nix_log:
            self.events.append(event)
        elif event.is_request_finalized:
            self._finalized.add(event.request_id)
            waiter = self._waiters.get(event.request_id)
            if waiter is not None:
                waiter.set()

    async def __aenter__(self) -> LogCapture:
        self._sub = self._bus.subscribe(self._append)
        self._active = True
        self._token = ACTIVE_LOG_CAPTURES.set((*ACTIVE_LOG_CAPTURES.get(), self))
        return self

    async def __aexit__(self, *args: object) -> None:
        self._active = False
        if self._token is not None:
            ACTIVE_LOG_CAPTURES.reset(self._token)  # type: ignore[arg-type] -- ContextVar token is opaque
            self._token = None
        try:
            with anyio.CancelScope(shield=True):
                await self.wait()
        finally:
            if self._sub is not None:
                self._sub.unsubscribe()
                self._sub = None
