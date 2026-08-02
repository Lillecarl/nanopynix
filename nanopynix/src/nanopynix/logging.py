"""Thread-safe bridge from Nix's C++ logger to Python consumers.

The C++ ``PyLogger`` calls the callback from *any* thread after
``gil_scoped_acquire``, so the callback must be thread-safe.  We use a
``janus.Queue`` which provides both a synchronous (thread-safe) interface
for the worker subprocess and an async interface for the Nix manager client.

The rule this module exists to keep:

    A log event may be lost. Nix's progress may not be delayed. Exactly one
    hop in each process is lossy, it sits at the process boundary, and every
    hop above it is guaranteed to drain. A control event is never lost.

A *control event* is a ``request_finalized`` marker or the stream sentinel. A
log line is diagnostic and a marker is protocol, which is why the two get
different treatment when a buffer fills.

It used to be the other way round. :meth:`LogCollector.callback` blocked until
the queue had room, and that queue was the head of an unbroken chain: the
worker relayed each event straight onto its gRPC stream, HTTP/2 flow control
parked that send when the client stopped reading, and the client stopped
reading whenever the caller's own event loop was busy. So a caller who ran
synchronous work stopped the Nix thread, and it looked like a hang in an
``await`` with no error anywhere. See issue #13.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import threading
import time
from collections import deque
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import anyio
import janus
from nanopynix_proto.nix.common import EventsDropped, LogEvent as LogEventProto

from nanopynix._typechecking import BEARTYPING
from nanopynix.models import LogEvent

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncIterator, Callable

    # `LogEvent | None`, and not bare `LogEvent`: `None` is the teardown
    # marker that tells a consumer the stream has ended. `CallbackBus.emit`
    # guarantees a subscriber sees nothing else -- see its docstring.
    type LogCallback = Callable[[LogEvent | None], None]

_logger = logging.getLogger(__name__)

_DROP_REPORT_INTERVAL_SECONDS = 30.0
"""How often a buffer that is discarding events says so through ``logging``.

Rate-limited because the condition arrives in bursts of thousands: one line
for each dropped event would itself be a load on whatever consumes Python's
logging. Each line carries the cumulative count, so nothing is hidden by the
interval."""

_OUTBOX_MAXSIZE = 10_000
"""How many log lines :class:`LogOutbox` holds before it discards the oldest.

The same order as ``LogCollector``'s default, because the two are the same
kind of buffer one hop apart."""

_OUTBOX_CEILING_FACTOR = 4
"""``maxsize`` multiplied by this bounds :class:`LogOutbox` in total.

A control event is never discarded to make room for a log line, so a stream
of nothing but control events could grow past ``maxsize``. That needs one
outstanding operation for each event, which no real caller produces, but a
buffer with no bound at all is not something to leave in place."""

_LOG_STREAM_BUFFER_EVENTS = 10_000
"""How many events one :func:`bus_log_stream` iterator holds for its caller.

The same order as ``LogCollector``'s default, because it is the same kind of
buffer one hop further on. rpc's copy of this stream used ``math.inf``, which
let a caller who iterates slowly grow the process without limit, and inproc's
copy still did after rpc corrected it. One function now, so a correction to
the rule reaches both engines."""

_CAPTURE_MAX_EVENTS = 100_000
"""How many events one :class:`LogCapture` keeps before it discards the oldest.

The oldest, not the newest: ``nanopynix_helpers.build`` and ``ekn.eval`` both
scan a capture for the fixed-output hash mismatch that Nix prints next to the
failure, so the end of the log is the part worth keeping."""

_CAPTURE_WAIT_TIMEOUT_SECONDS = 60.0
"""How long :class:`LogCapture` waits for a ``request_finalized`` marker.

Generous, because it is not a deadline on the work: ``__aexit__`` runs after
the operation already returned, so the marker it waits for is either in the
buffer or a few hops behind it. It is a bound on a wait that would otherwise
never end -- a marker lost upstream used to park a shielded ``await`` for the
life of the process."""

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
        self._dropped: int = 0
        self._undelivered_drops: int = 0
        self._last_drop_report: float = 0.0
        self._stats_lock = threading.Lock()

    # ── callback (thread-safe, called from C++ on any GIL thread) ──

    def callback(self, req_id: int, action: str, *args: object) -> None:
        """Push a log event onto the queue — thread-safe, and never blocks.

        The Nix thread calls this, so it must return in bounded time whatever
        the consumer is doing. A full queue therefore costs the event, not the
        evaluation: see :meth:`take_dropped` for how the loss is announced.

        There is no wait before the drop, not even a short one. A wait would
        only help when the task that drains this queue is itself stalled, and
        the drainer does nothing but move events, so a stall there is a defect
        that a timeout cannot repair.
        """
        self._put_log((LogStreamEventKind.NIX, req_id, action, *args))

    def request_finalized(self, request_id: int) -> None:
        """Enqueue the typed operation boundary after its Nix work finishes."""
        self._put_control((LogStreamEventKind.FINALIZED, request_id))

    def _put_log(self, item: Any) -> None:
        """Enqueue a droppable event."""
        try:
            self._queue.sync_q.put_nowait(item)
        except janus.SyncQueueFull:
            self._record_drop()
            return
        with self._stats_lock:
            self._enqueued += 1

    def _put_control(self, item: Any) -> None:
        """Enqueue an event that must arrive, by discarding one that need not.

        A lost ``request_finalized`` marker is not a lost log line. It parks
        :meth:`LogCapture.wait` until that wait's own bound expires, and it
        makes the capture report itself incomplete. So a control event takes
        the place of the oldest queued event rather than joining it in the
        drop count.

        Taking from the queue on the producing thread is safe here: a
        ``janus.SyncQueue`` is an ordinary thread-safe queue on this side, and
        it notifies the async side either way.
        """
        try:
            self._queue.sync_q.put_nowait(item)
        except janus.SyncQueueFull:
            # An empty queue cannot raise Full, so this only fails when the
            # consumer emptied it in between -- which leaves room anyway.
            with contextlib.suppress(janus.SyncQueueEmpty):
                self._queue.sync_q.get_nowait()
                self._record_drop()
            try:
                self._queue.sync_q.put_nowait(item)
            except janus.SyncQueueFull:
                # Another producer took the slot that was just freed. Rare,
                # and the alternative is a loop that a busy producer can keep
                # alive, so count it and move on.
                self._record_drop()
                return
        with self._stats_lock:
            self._enqueued += 1

    def _record_drop(self) -> None:
        with self._stats_lock:
            self._dropped += 1
            self._undelivered_drops += 1
            total = self._dropped
            now = time.monotonic()
            if now - self._last_drop_report < _DROP_REPORT_INTERVAL_SECONDS:
                return
            self._last_drop_report = now
        # Outside the lock: a logging handler is arbitrary code, and this runs
        # on the Nix thread.
        _logger.warning(
            "log queue is full; discarded %d event(s) so far. The consumer of this queue is not keeping up.",
            total,
        )

    def take_dropped(self) -> int:
        """Return the drops that no consumer knows about yet, and reset them.

        The task that drains this collector calls this and puts an
        ``EventsDropped`` event in the stream, so the loss reaches the caller
        at the point in the log where it happened. Exactly one task drains a
        collector, which is what makes a single counter enough.
        """
        with self._stats_lock:
            count, self._undelivered_drops = self._undelivered_drops, 0
            return count

    def stats(self) -> dict[str, int | bool]:
        """Return queue counters for worker signal diagnostics."""
        with self._stats_lock:
            enqueued = self._enqueued
            dropped = self._dropped
        return {
            "maxsize": self._maxsize,
            "qsize": self._queue.sync_q.qsize(),
            "full": self._queue.sync_q.full(),
            "empty": self._queue.sync_q.empty(),
            "enqueued": enqueued,
            "dropped": dropped,
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
        """Push a ``None`` sentinel to unblock ``stream()`` without closing.

        There was an ``asend_sentinel`` beside this, for callers already on an
        event loop, because the queue was bounded and this one could block. It
        cannot block any more -- the sentinel is a control event -- so the two
        did the same thing and only one is left.
        """
        self._put_control(None)


class LogOutbox:
    """Bounded hand-off from the worker's log relay to its gRPC stream.

    The second half of the rule at the top of this module. :class:`LogCollector`
    stops the Nix thread from waiting on anything; this stops the wire from
    reaching back into the collector.

    The relay task drains the collector unconditionally and puts each encoded
    event here. ``SubscribeLogs`` takes them out and sends them, and that send
    parks whenever HTTP/2 flow control says the client is not reading. The
    parked send now backs up into this buffer, which discards, instead of into
    the collector, which would stop Nix.

    :meth:`put` never blocks, and never discards a control event: a
    ``request_finalized`` marker is protocol and a log line is not. The
    sentinel (``None``) is a control event for the same reason.

    One consumer only. :meth:`get` replaces its own wake-up event, which is
    correct for one waiter and racy for two -- the same constraint
    ``SubscribeLogs`` already documents for the collector stream.
    """

    def __init__(self, maxsize: int = _OUTBOX_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._ceiling = maxsize * _OUTBOX_CEILING_FACTOR
        self._items: deque[LogEventProto | None] = deque()
        self._ready = anyio.Event()
        self._dropped = 0
        self._undelivered_drops = 0
        self._last_drop_report = 0.0

    def put(self, event: LogEventProto | None) -> None:
        """Add an event. Never blocks."""
        self._items.append(event)
        while len(self._items) > self._maxsize and self._discard_oldest_log():
            pass
        while len(self._items) > self._ceiling:
            self._items.popleft()
            self._record_drop()
        self._ready.set()

    def _discard_oldest_log(self) -> bool:
        """Remove the oldest log line, and report whether there was one.

        The scan is over the head of the buffer only in practice: it stops at
        the first log line, and a buffer that is over its size is nearly all
        log lines.
        """
        for index, item in enumerate(self._items):
            if item is not None and item.nix_log is not None:
                del self._items[index]
                self._record_drop()
                return True
        return False

    def _record_drop(self) -> None:
        self._dropped += 1
        self._undelivered_drops += 1
        now = time.monotonic()
        if now - self._last_drop_report < _DROP_REPORT_INTERVAL_SECONDS:
            return
        self._last_drop_report = now
        _logger.warning(
            "log outbox is full; discarded %d event(s) so far. The client is not reading its log stream.",
            self._dropped,
        )

    def take_dropped(self) -> int:
        """Return the drops that no consumer knows about yet, and reset them.

        ``SubscribeLogs`` calls this and sends an ``EventsDropped`` event, the
        same way the relay does for :meth:`LogCollector.take_dropped`. No lock,
        unlike the collector: this class only ever runs on the event loop.
        """
        count, self._undelivered_drops = self._undelivered_drops, 0
        return count

    def stats(self) -> dict[str, int]:
        """Return buffer counters, for the SIGUSR1 diagnostic dump."""
        return {"maxsize": self._maxsize, "pending": len(self._items), "dropped": self._dropped}

    async def get(self) -> LogEventProto | None:
        """Wait for the next event and return it."""
        while not self._items:
            await self._ready.wait()
            self._ready = anyio.Event()
        return self._items.popleft()


def events_dropped_event(count: int) -> LogEventProto:
    """Build the event that tells a consumer how many events it will not get.

    Request id 0, because a drop spans whatever operations were logging at the
    time and belongs to none of them. Both engines build it here, so the
    convention stays in one place.
    """
    return LogEventProto(request_id=0, events_dropped=EventsDropped(count=count))


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

    **This bus carries** :class:`~nanopynix.models.LogEvent` **, and nothing
    else but the** ``None`` **teardown marker.** ``emit`` normalises, so a
    producer may hand it the bare proto and a subscriber never sees one.

    That invariant is new, and it replaces an asymmetry that every consumer
    used to repeat. inproc emitted the model, rpc emitted the bare proto it
    subclasses, and ``events_dropped_event`` emitted a bare proto from *both*
    engines -- so each subscriber began with ``isinstance(raw, LogEventProto)``
    and ``LogEvent.from_proto(raw)``. That is a workaround, not an API, and it
    was not one a caller outside this repository could write: nanopynix
    exports the subclass and not the base, so ``ekn`` reached into
    ``nanopynix_proto`` to get a class to test against. Normalising once here
    is what lets :meth:`Session.subscribe` state the type it delivers.
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
        """Dispatch one event, normalised to the model, to every subscriber.

        Anything that is neither a log event nor the ``None`` marker is
        dropped and logged. It is **not** turned into ``None``: that is the
        teardown marker, and coercing an unexpected object into it would tell
        every subscriber the stream had ended. A producer that emits the wrong
        thing is a bug, and it should look like one.
        """
        if not self._subscribers:
            return
        if event is None:
            normalised = None
        elif isinstance(event, LogEvent):
            normalised = event
        elif isinstance(event, LogEventProto):
            # Normalise once, not once per subscriber: they all want the same
            # model, and `from_proto` builds a new object each time.
            normalised = LogEvent.from_proto(event)
        else:
            _logger.error("log bus discarded a %s; this bus carries LogEvent", type(event).__name__)
            return
        for callback in tuple(self._subscribers):
            try:
                callback(normalised)
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
    events differ only in how they got here, and a subscriber receives the
    same :class:`~nanopynix.models.LogEvent` either way.

    ``CallbackBus.emit`` is what makes that true, and it used to be only
    half-true -- see its docstring for the asymmetry this replaced.
    """

    def subscribe(self, callback: LogCallback) -> BusSubscription: ...


async def bus_log_stream(bus: LogEventBus) -> AsyncIterator[LogEvent]:
    """Iterate one log bus as an async stream. Both engines' ``log_stream``.

    Bounded, and it discards the oldest event when the caller does not keep
    up. That is the rule at the top of this module, applied at the last hop:
    the caller loses a log line, and the dispatch that feeds it never blocks.

    The iterator ends when the bus emits the ``None`` teardown marker, so a
    caller who iterates a session to the end returns instead of waiting for
    an event that will not arrive.

    Both engines called their own copy of this until now, and the two copies
    had drifted: rpc bounded its buffer and reported the loss, inproc used
    ``math.inf`` and reported nothing, and only rpc stopped on the marker.
    """
    send_stream, receive_stream = anyio.create_memory_object_stream[LogEvent | None](
        max_buffer_size=_LOG_STREAM_BUFFER_EVENTS
    )

    dropped = 0
    last_report = 0.0

    def _on_event(event: LogEvent | None) -> None:
        nonlocal dropped, last_report
        try:
            send_stream.send_nowait(event)
        except anyio.WouldBlock:
            pass
        else:
            return
        # `emit` runs on the event loop and must not block, so a full buffer
        # costs the oldest event rather than the dispatch. Neither call below
        # can fail: a full buffer always has something to take, and taking one
        # always leaves room. Both run with no await in between, so nothing
        # can get in between them.
        receive_stream.receive_nowait()
        send_stream.send_nowait(event)
        dropped += 1
        now = time.monotonic()
        if now - last_report >= _DROP_REPORT_INTERVAL_SECONDS:
            last_report = now
            _logger.warning("log_stream buffer is full; discarded %d event(s) so far", dropped)

    sub = bus.subscribe(_on_event)
    try:
        async for event in receive_stream:
            if event is None:
                break
            yield event
    finally:
        sub.unsubscribe()


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

    A capture says when it is not complete rather than pretending otherwise.
    :attr:`truncated` reports that this capture reached ``max_events`` and
    discarded its own oldest events. :attr:`dropped_events` reports events lost
    further up, which the producer announces with an ``EventsDropped`` event.
    :attr:`unfinalized_request_ids` reports the operations whose marker never
    arrived within ``wait_timeout``.
    """

    def __init__(
        self,
        bus: LogEventBus,
        *,
        max_events: int | None = None,
        wait_timeout: float | None = None,
    ) -> None:
        # `None` rather than the constant as the default, so that both
        # `capture_logs` methods can forward "the caller said nothing" without
        # naming a value of their own. Two engines naming the same default is
        # two places for it to drift.
        self._max_events = _CAPTURE_MAX_EVENTS if max_events is None else max_events
        self._wait_timeout = _CAPTURE_WAIT_TIMEOUT_SECONDS if wait_timeout is None else wait_timeout
        self._bus = bus
        self._sub: BusSubscription | None = None
        # A deque, and not a list, because the cap must discard the oldest
        # event: see _CAPTURE_MAX_EVENTS for who reads the end of a log.
        self.events: deque[LogEvent] = deque(maxlen=self._max_events)
        self._truncated = False
        self._dropped_events = 0
        self._request_ids: set[int] = set()
        self._finalized: set[int] = set()
        self._waiters: dict[int, anyio.Event] = {}
        self._active = False
        self._token: object | None = None

    @property
    def request_ids(self) -> frozenset[int]:
        """Snapshot of operation IDs started inside this capture's scope."""
        return frozenset(self._request_ids)

    @property
    def truncated(self) -> bool:
        """Did this capture reach ``max_events`` and discard its own oldest?"""
        return self._truncated

    @property
    def dropped_events(self) -> int:
        """How many events were lost before they reached this capture.

        A different fact from :attr:`truncated`, which is this capture's own
        cap. This one counts what the collector or the worker's outbox threw
        away to keep Nix running.
        """
        return self._dropped_events

    @property
    def unfinalized_request_ids(self) -> frozenset[int]:
        """Operations started in this scope whose finalize marker never came."""
        return frozenset(self._request_ids - self._finalized)

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

    async def _await_request(self, request_id: int) -> bool:
        """Wait for one marker, bounded. Report whether it arrived.

        Bounded because the marker can be lost: the producer discards a
        control event only under a hard ceiling, but the client also stops
        reading its log stream during teardown while the worker still emits.
        This await used to have no bound and no cancellation -- ``__aexit__``
        holds it inside ``CancelScope(shield=True)`` -- so one lost marker
        parked the caller for the life of the process.
        """
        if request_id in self._finalized:
            return True
        with anyio.move_on_after(self._wait_timeout):
            await self._waiters[request_id].wait()
            return True
        return False

    async def wait_for_request(self, request_id: int) -> None:
        if request_id not in self._request_ids:
            raise ValueError(f"request {request_id} is not registered with this log capture")
        if not await self._await_request(request_id):
            raise TimeoutError(f"request {request_id} did not finalize within {self._wait_timeout}s")

    async def wait(self) -> None:
        """Wait for every operation started in this scope, bounded.

        Does not raise where :meth:`wait_for_request` does. ``__aexit__`` calls
        this, and an exception from ``__aexit__`` would replace whatever the
        caller's own block raised. :attr:`unfinalized_request_ids` reports the
        same fact without that cost.
        """
        request_ids = tuple(self._request_ids)
        async with anyio.create_task_group() as tg:
            for request_id in request_ids:
                tg.start_soon(self._await_request, request_id)

    def _append(self, event: LogEvent | None) -> None:
        # `None` is the teardown marker, and the bus normalises everything
        # else to the model -- so this no longer converts, and no longer needs
        # the bare proto class to test against.
        if event is None:
            return
        if event.is_nix_log:
            if len(self.events) == self._max_events:
                self._truncated = True
            self.events.append(event)
        elif event.is_request_finalized:
            self._finalized.add(event.request_id)
            waiter = self._waiters.get(event.request_id)
            if waiter is not None:
                waiter.set()
        elif event.is_events_dropped:
            self._dropped_events += event.dropped_count

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
                unfinalized = self.unfinalized_request_ids
                if unfinalized:
                    _logger.warning(
                        "log capture exited with %d operation(s) that never finalized: %s. "
                        "The captured events are incomplete.",
                        len(unfinalized),
                        sorted(unfinalized),
                    )
        finally:
            if self._sub is not None:
                self._sub.unsubscribe()
                self._sub = None
