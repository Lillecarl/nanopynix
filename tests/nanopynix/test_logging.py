"""Tests for the PyLogger log streaming with LogCollector."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# LogCollector, nanopynix_util.{install_logger,remove_logger,get/set_verbosity,set_logger_request_id}
# are C++ nanobind extension functions without type stubs; all member/variable types are Unknown.

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Protocol, cast

import nanopynix_util
from nanopynix import LogCollector

if TYPE_CHECKING:
    from collections.abc import Callable


class _LogTestModule(Protocol):
    def _log_test(self, msg: str) -> None: ...


_log_test: Callable[[str], None] = cast("_LogTestModule", nanopynix_util)._log_test  # type: ignore[reportPrivateUsage] -- test imports private helper


async def _collect(collector: LogCollector, count: int, timeout: float = 2.0) -> list[tuple[int, str, int, str]]:  # noqa: ASYNC109
    """Collect `count` events from the async stream."""
    events: list[tuple[int, str, int, str]] = []
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
            assert e[0] == 0, f"Expected req_id=0, got {e[0]}"
            assert e[1] == "msg", f"Expected 'msg' action, got {e[1]}"
            assert isinstance(e[2], int), f"level should be int, got {type(e[2])}"
            assert isinstance(e[3], str), f"msg should be str, got {type(e[3])}"

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

    assert event[1] == "msg"
    assert event[3] == "action test"


async def test_log_stream_remove_logger_stops():
    """After remove_logger, the callback should not receive events."""
    c = LogCollector()
    nanopynix_util.install_logger(c.callback)

    _log_test("before remove")
    stream = c.stream()
    event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
    assert event[3] == "before remove"

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
    assert [i[3] for i in items] == ["msg1", "msg2"]

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
        assert items == [], f"Expected no events at Error verbosity, got {[i[3] for i in items]}"
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
        assert event[0] == 42, f"Expected req_id=42, got {event[0]}"
        assert event[3] == "tagged"

        # Unset should produce req_id=0
        _log_test("untagged")
        event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert event[0] == 0
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
    assert events[0] == (1, "msg", 3, "hello")
    assert events[1] == (2, "msg", 3, "world")

    # Second drain should be empty (events already consumed)
    assert c.drain() == []

    c.close()


async def test_drain_empty():
    """drain() returns empty list when no events pending."""
    c = LogCollector()
    assert c.drain() == []
    c.close()
