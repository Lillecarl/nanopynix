"""Tests for the PyLogger log streaming with LogCollector."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# LogCollector, nanopynix_util.{install_logger,remove_logger,get/set_verbosity,set_logger_request_id}
# are C++ nanobind extension functions without type stubs; all member/variable types are Unknown.

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Protocol, cast

import anyio
import anyio.lowlevel
import anyio.to_thread
import pytest
from nanopynix_bindings import util as nanopynix_util
from nanopynix_proto.nix.common import LogEvent as LogEventProto, NixLogEvent, RequestFinalized

from nanopynix import LogCollector
from nanopynix.logging import (
    _OUTBOX_CEILING_FACTOR,  # type: ignore[reportPrivateUsage] -- the test pins the ceiling this constant sets
    CallbackBus,
    LogCapture,
    LogOutbox,
    bus_log_stream,
    events_dropped_event,
)
from nanopynix.models import LogEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from nanopynix.logging import BusSubscription


class _LogTestModule(Protocol):
    def _log_test(self, msg: str) -> None: ...


_log_test: Callable[[str], None] = cast("_LogTestModule", nanopynix_util)._log_test  # type: ignore[reportPrivateUsage] -- test imports private helper


async def _collect(collector: LogCollector, count: int, timeout: float = 2.0) -> list[tuple[str, int, str, int, str]]:  # noqa: ASYNC109 -- timeout is passed straight through to asyncio.wait_for, which accepts a timeout parameter
    """Collect `count` events from the async stream."""
    events: list[tuple[str, int, str, int, str]] = []
    stream = collector.stream()
    try:
        for _ in range(count):
            event = await asyncio.wait_for(stream.__anext__(), timeout=timeout)
            events.append(event)
    except (TimeoutError, StopAsyncIteration):
        pass
    return events


async def test_log_stream_basic():
    """LogCollector yields messages emitted via _log_test."""
    c = LogCollector()
    nanopynix_util.install_logger(c.callback)

    try:
        _log_test("hello from nix")
        _log_test("second message")
        _log_test("third message")

        events = await _collect(c, 3)

        assert len(events) == 3, f"Expected 3 events, got {len(events)}: {events}"
        for e in events:
            assert isinstance(e, tuple)
            assert e[0] == "nix"
            assert e[1] == 0, f"Expected req_id=0, got {e[1]}"
            assert e[2] == "msg", f"Expected 'msg' action, got {e[2]}"
            assert isinstance(e[3], int), f"level should be int, got {type(e[3])}"
            assert isinstance(e[4], str), f"msg should be str, got {type(e[4])}"

    finally:
        nanopynix_util.remove_logger()
        await c.aclose()


async def test_log_stream_actions():
    """Log messages have the expected action and content."""
    c = LogCollector()
    nanopynix_util.install_logger(c.callback)

    _log_test("action test")

    stream = c.stream()
    event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)

    nanopynix_util.remove_logger()
    await c.aclose()

    assert event[2] == "msg"
    assert event[4] == "action test"


async def test_log_stream_remove_logger_stops():
    """After remove_logger, the callback should not receive events."""
    c = LogCollector()
    nanopynix_util.install_logger(c.callback)

    _log_test("before remove")
    stream = c.stream()
    event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
    assert event[4] == "before remove"

    nanopynix_util.remove_logger()
    await c.aclose()

    remaining = [e async for e in stream]
    assert remaining == [], f"Expected [], got {remaining}"


async def test_log_stream_shutdown_clean():
    """After sentinel + close, remaining items drain cleanly."""
    c = LogCollector()
    nanopynix_util.install_logger(c.callback)

    _log_test("msg1")
    _log_test("msg2")

    nanopynix_util.remove_logger()

    # Signal end-of-stream with sentinel, then drain
    c.send_sentinel()
    stream = c.stream()
    items = [e async for e in stream]
    assert [i[4] for i in items] == ["msg1", "msg2"]

    await c.aclose()


async def test_verbosity_filters_low_levels():
    """Setting verbosity to Error (0) should suppress Info-level messages."""
    c = LogCollector()
    nanopynix_util.install_logger(c.callback)

    old = nanopynix_util.get_verbosity()
    try:
        nanopynix_util.set_verbosity(0)  # lvlError

        _log_test("should be dropped")

        nanopynix_util.remove_logger()
        nanopynix_util.set_verbosity(old)
        await c.aclose()

        stream = c.stream()
        items = [e async for e in stream]
        assert items == [], f"Expected no events at Error verbosity, got {[i[4] for i in items]}"
    finally:
        nanopynix_util.set_verbosity(old)
        with contextlib.suppress(Exception):
            nanopynix_util.remove_logger()
        await c.aclose()


async def test_request_id_in_events():
    """set_logger_request_id tags events correctly."""
    c = LogCollector()
    nanopynix_util.install_logger(c.callback)

    try:
        nanopynix_util.set_logger_request_id(42)
        _log_test("tagged")
        nanopynix_util.set_logger_request_id(0)

        stream = c.stream()
        event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert event[1] == 42, f"Expected req_id=42, got {event[1]}"
        assert event[4] == "tagged"

        # Unset should produce req_id=0
        _log_test("untagged")
        event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert event[1] == 0
    finally:
        nanopynix_util.remove_logger()
        await c.aclose()


async def test_sync_drain():
    """drain() returns pending events without blocking — used by worker subprocess."""
    c = LogCollector()
    c.callback(1, "msg", 3, "hello")
    c.callback(2, "msg", 3, "world")

    events = c.drain()
    assert len(events) == 2
    assert events[0] == ("nix", 1, "msg", 3, "hello")
    assert events[1] == ("nix", 2, "msg", 3, "world")

    # Second drain should be empty (events already consumed)
    assert c.drain() == []

    c.close()


async def test_drain_empty():
    """drain() returns empty list when no events pending."""
    c = LogCollector()
    assert c.drain() == []
    c.close()


# ── The rule: a log event may be lost, Nix's progress may not be delayed ──
#
# Issue #13. Each test below pins one clause of the rule stated at the top of
# nanopynix/logging.py. The first is the one that matters: before this, a
# consumer that stopped reading stopped the Nix thread inside the C++ logger
# callback, and it presented as a hang in an `await` with no error anywhere.


async def test_a_consumer_that_never_drains_does_not_stop_nix():
    """The acceptance criterion of issue #13.

    Drives the real C++ logger, because the defect was in what the logger
    callback does to the thread that calls it -- a queue exercised directly
    would not have caught it. `_log_test` goes through `nix::Logger`, the same
    path an evaluation takes.

    From a thread, under a deadline, so that the old blocking `put` reports a
    TimeoutError here instead of hanging the whole suite. Removing the drop
    from `LogCollector._put_log` makes this fail.
    """
    c = LogCollector(maxsize=8)
    nanopynix_util.install_logger(c.callback)
    try:
        with anyio.fail_after(30):
            await anyio.to_thread.run_sync(_emit_many, 50, abandon_on_cancel=True)
    finally:
        nanopynix_util.remove_logger()

    stats = c.stats()
    assert stats["dropped"] > 0, stats
    assert stats["qsize"] == 8, stats
    c.close()


def _emit_many(count: int) -> None:
    """Emit `count` log lines through Nix's own logger. Runs in a thread."""
    for index in range(count):
        _log_test(f"line {index}")


async def test_a_finalize_marker_takes_the_place_of_a_log_line():
    """A control event is never lost; a log line is.

    A lost marker is not a lost log line. `LogCapture.wait` blocks on it, so
    losing one turns a full buffer into an incomplete capture. Dropping the
    eviction in `_put_control` makes this fail.
    """
    c = LogCollector(maxsize=3)
    for index in range(3):
        c.callback(1, "msg", 3, f"line {index}")
    assert c.stats()["full"] is True

    c.request_finalized(7)

    kinds = [event[0] for event in c.drain()]
    assert kinds == ["nix", "nix", "finalized"]
    assert c.stats()["dropped"] == 1
    c.close()


async def test_the_drop_report_is_rate_limited(caplog: pytest.LogCaptureFixture):
    """One line for each dropped event would itself be the load.

    The condition arrives in bursts of thousands. Each line carries the
    cumulative count, so the interval hides nothing.
    """
    c = LogCollector(maxsize=1)
    with caplog.at_level(logging.WARNING, logger="nanopynix.logging"):
        for index in range(200):
            c.callback(1, "msg", 3, f"line {index}")

    assert c.stats()["dropped"] == 199
    warnings = [record for record in caplog.records if "log queue is full" in record.message]
    assert len(warnings) == 1, [record.message for record in warnings]
    c.close()


async def test_take_dropped_reports_a_delta_and_resets():
    """The relay turns each delta into one EventsDropped event on the wire."""
    c = LogCollector(maxsize=1)
    for index in range(4):
        c.callback(1, "msg", 3, f"line {index}")

    assert c.take_dropped() == 3
    assert c.take_dropped() == 0

    c.callback(1, "msg", 3, "one more")
    assert c.take_dropped() == 1
    c.close()


# ── LogOutbox — the worker's bounded hand-off to its gRPC stream ─────────


def _log_event(request_id: int) -> LogEventProto:
    return LogEventProto(request_id=request_id, nix_log=NixLogEvent(action="msg", args_json="[]"))


async def test_the_outbox_discards_the_oldest_log_line():
    """The oldest, because the end of a log is the part worth keeping.

    ``nanopynix_helpers.build`` and ``ekn.eval`` both scan a capture for the
    fixed-output hash mismatch Nix prints next to the failure.
    """
    outbox = LogOutbox(maxsize=3)
    for request_id in range(6):
        outbox.put(_log_event(request_id))

    with anyio.fail_after(5):
        kept = [await outbox.get() for _ in range(3)]

    assert [event.request_id for event in kept if event is not None] == [3, 4, 5]
    assert outbox.stats()["dropped"] == 3


async def test_the_outbox_keeps_every_control_event():
    """Same rule as the collector, one hop further on.

    Both control events survive a run of log lines longer than the whole
    buffer, and they push every log line out rather than the other way round.
    """
    outbox = LogOutbox(maxsize=2)
    outbox.put(LogEventProto(request_id=1, request_finalized=RequestFinalized()))
    for request_id in range(10, 16):
        outbox.put(_log_event(request_id))
    outbox.put(None)

    assert outbox.stats()["pending"] == 2
    with anyio.fail_after(5):
        first = await outbox.get()
        second = await outbox.get()

    assert first is not None
    assert first.request_finalized is not None
    assert second is None


async def test_the_outbox_is_bounded_even_with_no_log_lines_to_discard():
    """A buffer with no bound at all is not something to leave in place.

    Needs one outstanding operation for each event, which no real caller
    produces -- but ``_OUTBOX_CEILING_FACTOR`` exists so the pathological case
    still has an answer.
    """
    outbox = LogOutbox(maxsize=2)
    for request_id in range(50):
        outbox.put(LogEventProto(request_id=request_id, request_finalized=RequestFinalized()))

    assert outbox.stats()["pending"] == 2 * _OUTBOX_CEILING_FACTOR
    assert outbox.stats()["dropped"] > 0


# ── LogCapture — bounded, and it says when it is incomplete ──────────────


class _FakeBus:
    """The one method :class:`LogEventBus` needs."""

    def __init__(self) -> None:
        self.bus = CallbackBus()

    def subscribe(self, callback: Callable[..., None]) -> BusSubscription:
        return self.bus.subscribe(callback)


async def test_a_capture_keeps_the_newest_events_and_says_it_truncated():
    bus = _FakeBus()
    async with LogCapture(bus, max_events=3) as capture:
        for request_id in range(10):
            bus.bus.emit(_log_event(request_id))

    assert [event.request_id for event in capture.events] == [7, 8, 9]
    assert capture.truncated is True


async def test_a_capture_under_its_cap_is_not_truncated():
    """The control for the test above."""
    bus = _FakeBus()
    async with LogCapture(bus, max_events=3) as capture:
        bus.bus.emit(_log_event(1))

    assert capture.truncated is False


async def test_a_capture_counts_what_was_lost_upstream():
    """`truncated` is this capture's own cap; `dropped_events` is not."""
    bus = _FakeBus()
    async with LogCapture(bus) as capture:
        bus.bus.emit(events_dropped_event(12))
        bus.bus.emit(events_dropped_event(3))

    assert capture.dropped_events == 15
    assert capture.truncated is False


async def test_a_capture_reports_a_marker_that_never_arrives():
    """The wait used to have no bound, inside a scope that shields it.

    So one lost marker parked the caller for the life of the process. The
    client already loses markers: `WorkerClient._teardown` cancels its log
    task after two seconds while the worker is still emitting.
    """
    bus = _FakeBus()
    with anyio.fail_after(10):
        async with LogCapture(bus, wait_timeout=0.05) as capture:
            capture._register_request(42)  # type: ignore[reportPrivateUsage] -- the ACTIVE_LOG_CAPTURES contract, which needs a live engine

    assert capture.unfinalized_request_ids == frozenset({42})


async def test_waiting_for_one_named_request_raises_when_it_never_finalizes():
    """`wait_for_request` raises where `wait` only records.

    An explicit call deserves an explicit failure. `wait` cannot raise: it runs
    from `__aexit__`, where an exception would replace whatever the caller's
    own block raised.
    """
    bus = _FakeBus()
    capture = LogCapture(bus, wait_timeout=0.05)
    async with capture:
        capture._register_request(42)  # type: ignore[reportPrivateUsage] -- see above
        with pytest.raises(TimeoutError, match="did not finalize"):
            await capture.wait_for_request(42)


async def test_a_marker_that_arrives_ends_the_wait():
    """The control: the bound is a backstop, not the normal path."""
    bus = _FakeBus()
    async with LogCapture(bus, wait_timeout=30.0) as capture:
        capture._register_request(42)  # type: ignore[reportPrivateUsage] -- see above
        bus.bus.emit(LogEventProto(request_id=42, request_finalized=RequestFinalized()))

    assert capture.unfinalized_request_ids == frozenset()


# ── CallbackBus — shared pub-sub used by inproc.Session and the RPC
# client's WorkerClient (see nanopynix.logging.CallbackBus's docstring for
# why the worker's own subscribe_logs is not built on this). ──────────────


def _ids(seen: list[LogEvent | None]) -> list[int | None]:
    """Request ids, with `None` kept for the teardown marker."""
    return [None if event is None else event.request_id for event in seen]


def test_callback_bus_dispatches_to_all_subscribers():
    bus = CallbackBus()
    seen_a: list[LogEvent | None] = []
    seen_b: list[LogEvent | None] = []
    bus.subscribe(seen_a.append)
    bus.subscribe(seen_b.append)

    bus.emit(_log_event(1))

    assert _ids(seen_a) == [1]
    assert _ids(seen_b) == [1]


def test_callback_bus_normalises_the_wire_event_to_the_model():
    """Both engines' subscribers see the model, never the proto it subclasses.

    This is the invariant that lets `Session.subscribe` state its type, and
    that removed ekn's need to import the proto class to narrow against.
    """
    bus = CallbackBus()
    seen: list[LogEvent | None] = []
    bus.subscribe(seen.append)

    bus.emit(_log_event(1))

    assert type(seen[0]) is LogEvent


def test_callback_bus_passes_the_teardown_marker_through() -> None:
    bus = CallbackBus()
    seen: list[LogEvent | None] = []
    bus.subscribe(seen.append)

    bus.emit(None)

    assert seen == [None]


def test_callback_bus_drops_a_foreign_object_rather_than_calling_it_teardown() -> None:
    """A producer bug must not read as "the stream ended".

    `None` means teardown. Coercing an unexpected object into it would tell
    every subscriber to stop, so `emit` drops and logs instead.
    """
    bus = CallbackBus()
    seen: list[LogEvent | None] = []
    bus.subscribe(seen.append)

    bus.emit("not an event")

    assert seen == []


def test_callback_bus_unsubscribe_stops_dispatch():
    bus = CallbackBus()
    seen: list[LogEvent | None] = []
    sub = bus.subscribe(seen.append)

    bus.emit(_log_event(1))
    sub.unsubscribe()
    bus.emit(_log_event(2))

    assert _ids(seen) == [1]


def test_callback_bus_raising_subscriber_does_not_block_others():
    """A subscriber that raises is logged, not fatal to the remaining subscribers.

    This is a deliberate behavior change from the old inproc-private
    dispatch loop it replaced (which let a raising callback propagate and
    kill the session's log-forwarding task) -- unified onto the RPC
    client's more robust swallow-and-log semantics.
    """
    bus = CallbackBus()
    seen: list[LogEvent | None] = []

    def _raises(_event: object) -> None:
        raise RuntimeError("boom")

    bus.subscribe(_raises)
    bus.subscribe(seen.append)

    bus.emit(_log_event(1))

    assert _ids(seen) == [1]


def test_callback_bus_same_callback_subscribed_twice_dispatches_twice():
    """Insertion-ordered list semantics: duplicate subscriptions are independent.

    A deliberate change from inproc's old set-based dispatch (which
    deduplicated by identity), matching the RPC client's list-based bus.
    """
    bus = CallbackBus()
    seen: list[LogEvent | None] = []
    first = bus.subscribe(seen.append)
    bus.subscribe(seen.append)

    bus.emit(_log_event(1))
    assert _ids(seen) == [1, 1]

    first.unsubscribe()
    seen.clear()
    bus.emit(_log_event(1))
    assert _ids(seen) == [1]


# ── bus_log_stream — the one `log_stream` both engines return ────────────
#
# It replaced a copy in each engine, and the two copies had drifted: rpc
# bounded its buffer and stopped on the teardown marker, inproc used
# `math.inf` and never stopped at all.


async def _drain_bus(bus: CallbackBus, events: list[LogEventProto | None]) -> list[int]:
    """Emit *events* onto *bus* while one `bus_log_stream` iterates it.

    The list must carry the `None` marker, or the iterator never ends.
    """

    async def _emit() -> None:
        # One checkpoint first: the bus discards an event that arrives with
        # nobody subscribed, and the iterator subscribes on its first
        # `__anext__`. Nothing after it, so the whole list lands before the
        # reader runs again -- which is what makes the drop test deterministic.
        await anyio.lowlevel.checkpoint()
        for event in events:
            bus.emit(event)

    seen: list[LogEvent] = []
    async with anyio.create_task_group() as tg:
        tg.start_soon(_emit)
        seen.extend([event async for event in bus_log_stream(bus)])
    return [event.request_id for event in seen]


async def test_bus_log_stream_ends_on_the_teardown_marker():
    """The marker ends the iterator, and nothing after it is delivered."""
    seen = await _drain_bus(CallbackBus(), [_log_event(1), _log_event(2), None, _log_event(3)])
    assert seen == [1, 2]


async def test_bus_log_stream_discards_the_oldest_when_the_caller_falls_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule at the top of the module, applied at the last hop.

    A log line is lost and the dispatch that feeds it is not delayed. inproc's
    copy of this stream buffered without a bound, so a slow caller grew the
    process instead.
    """
    monkeypatch.setattr("nanopynix.logging._LOG_STREAM_BUFFER_EVENTS", 3)
    seen = await _drain_bus(CallbackBus(), [*(_log_event(i) for i in range(6)), None])

    assert len(seen) < 6, "nothing was discarded, so the buffer is not bounded"
    assert seen[-2:] == [4, 5], "the newest events are the ones that survived"
