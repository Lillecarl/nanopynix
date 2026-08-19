"""A progress line stops repeating itself as the run goes on.

A tick carries three facts: the run is alive, it is making progress, and this
is the test it is on. All three arrive on the first one, and a fixed interval
makes the count a function of the wall clock -- a 474 s run printed 46 lines,
which is the useful seven, six more times.

The rule holds at the base long enough that a short run is exactly as
informative as it was, and doubles after that. A stuck run is not what this
watches: `--agent-stuck-after` owns that, and it dumps every thread's stack
rather than printing one more line that says what the last one said.

Issue #46.
"""

from __future__ import annotations

from pytest_agent._runtime import (
    DEFAULT_HEARTBEAT_INTERVAL,
    HEARTBEAT_CAP,
    HEARTBEAT_HOLD,
    next_heartbeat,
)


def _next(every: float, elapsed: float, *, backoff: bool = True) -> float:
    """The rule alone, with nothing of a run around it."""
    return next_heartbeat(every, elapsed, backoff=backoff)


def _ticks(total: float, *, backoff: bool) -> int:
    """How many progress lines a run of `total` seconds prints."""
    every = DEFAULT_HEARTBEAT_INTERVAL
    at = 0.0
    count = 0
    while at + every <= total:
        at += every
        count += 1
        every = _next(every, at, backoff=backoff)
    return count


def test_the_interval_holds_at_its_base_while_a_run_is_short() -> None:
    """A 70 s run prints what it printed before this rule existed."""
    assert _next(DEFAULT_HEARTBEAT_INTERVAL, 10.0) == DEFAULT_HEARTBEAT_INTERVAL
    assert _next(DEFAULT_HEARTBEAT_INTERVAL, HEARTBEAT_HOLD - 1) == DEFAULT_HEARTBEAT_INTERVAL
    assert _ticks(74.0, backoff=True) == _ticks(74.0, backoff=False) == 7


def test_the_interval_doubles_after_the_hold_and_stops_at_the_cap() -> None:
    assert _next(DEFAULT_HEARTBEAT_INTERVAL, HEARTBEAT_HOLD) == 20.0
    assert _next(20.0, 200.0) == 40.0
    assert _next(40.0, 300.0) == HEARTBEAT_CAP
    assert _next(HEARTBEAT_CAP, 1000.0) == HEARTBEAT_CAP


def test_a_long_run_prints_a_dozen_lines_rather_than_forty_six() -> None:
    """The measurement of the issue: 474 s, and 46 lines before this."""
    assert _ticks(474.0, backoff=False) == 47
    assert 10 <= _ticks(474.0, backoff=True) <= 16


def test_a_named_interval_does_not_widen() -> None:
    """`--agent-heartbeat 5` means five seconds, and keeps meaning it."""
    assert _next(5.0, 1000.0, backoff=False) == 5.0
