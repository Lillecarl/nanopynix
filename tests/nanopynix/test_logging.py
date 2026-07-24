"""Tests for the PyLogger log streaming with LogCollector."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# LogCollector, nanopynix_util.{install_logger,remove_logger,get/set_verbosity,set_logger_request_id}
# are C++ nanobind extension functions without type stubs; all member/variable types are Unknown.

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Protocol, cast

from nanopynix_bindings import util as nanopynix_util

from nanopynix import LogCollector
from nanopynix.logging import CallbackBus

if TYPE_CHECKING:
    from collections.abc import Callable


class _LogTestModule(Protocol):
    def _log_test(self, msg: str) -> None: ...


_log_test: Callable[[str], None] = cast("_LogTestModule", nanopynix_util)._log_test  # type: ignore[reportPrivateUsage] -- test imports private helper


async def _collect(collector: LogCollector, count: int, timeout: float = 2.0) -> list[tuple[str, int, str, int, str]]:  # noqa: ASYNC109
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


# ── CallbackBus — shared pub-sub used by inproc.Session and the RPC
# client's WorkerClient (see nanopynix.logging.CallbackBus's docstring for
# why the worker's own subscribe_logs is not built on this). ──────────────


def test_callback_bus_dispatches_to_all_subscribers():
    bus = CallbackBus()
    seen_a: list[object] = []
    seen_b: list[object] = []
    bus.subscribe(seen_a.append)
    bus.subscribe(seen_b.append)

    bus.emit("event")

    assert seen_a == ["event"]
    assert seen_b == ["event"]


def test_callback_bus_unsubscribe_stops_dispatch():
    bus = CallbackBus()
    seen: list[object] = []
    sub = bus.subscribe(seen.append)

    bus.emit("first")
    sub.unsubscribe()
    bus.emit("second")

    assert seen == ["first"]


def test_callback_bus_raising_subscriber_does_not_block_others():
    """A subscriber that raises is logged, not fatal to the remaining subscribers.

    This is a deliberate behavior change from the old inproc-private
    dispatch loop it replaced (which let a raising callback propagate and
    kill the session's log-forwarding task) -- unified onto the RPC
    client's more robust swallow-and-log semantics.
    """
    bus = CallbackBus()
    seen: list[object] = []

    def _raises(_event: object) -> None:
        raise RuntimeError("boom")

    bus.subscribe(_raises)
    bus.subscribe(seen.append)

    bus.emit("event")

    assert seen == ["event"]


def test_callback_bus_same_callback_subscribed_twice_dispatches_twice():
    """Insertion-ordered list semantics: duplicate subscriptions are independent.

    A deliberate change from inproc's old set-based dispatch (which
    deduplicated by identity), matching the RPC client's list-based bus.
    """
    bus = CallbackBus()
    seen: list[object] = []
    first = bus.subscribe(seen.append)
    bus.subscribe(seen.append)

    bus.emit("event")
    assert seen == ["event", "event"]

    first.unsubscribe()
    seen.clear()
    bus.emit("event")
    assert seen == ["event"]
