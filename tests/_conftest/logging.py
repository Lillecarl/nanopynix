"""Structured logging configuration and per-test log file fixtures.

Uses a single autouse fixture (``test_logging``) that:
1. Captures every structlog event into a per-test list
2. Writes formatted output to a per-test log file
3. Prints statistics on teardown (total events, dropped, top sources)
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

import pytest
import structlog
from structlog import DropEvent

from tests._conftest.constants import _log_dir_key

# ── Life counter: cooperative vote-to-drop system ────────────────

_LIFE_KEY = "_pynixd_life"

_dropped_counter: Counter[tuple[str, str, str]] = Counter()


def _life_check_processor(logger: Any, method_name: str, event_dict: Any) -> Any:
    """Final gate — drop events whose life counter is negative."""
    life = event_dict.pop(_LIFE_KEY, 0)
    if life < 0:
        key = (method_name, str(event_dict.get("logger", "")), str(event_dict.get("event", "")))
        _dropped_counter[key] += 1
        raise DropEvent
    return event_dict


# ── Capture: snapshot every event for per-test statistics ─────────

_captured_events: list[dict[str, Any]] = []


def _capture_processor(logger: Any, method_name: str, event_dict: Any) -> Any:
    """Snapshot every event before _life_check_processor can drop it.

    Placed right before _life_check_processor in the chain so dropped
    events are also countable.  Always passes through — never raises DropEvent.
    """
    _captured_events.append(dict(event_dict))
    return event_dict


# ── Keys to exclude from per-event example lines ─────────────────

_SKIP_KEYS = frozenset(
    {
        "event",
        "logger",
        "level",
        "log_level",
        "timestamp",
        _LIFE_KEY,
        "exception",
        "stack",
    }
)


# ── Time stampers ────────────────────────────────────────────────

_session_start_time = time.monotonic()


def _abs_time_stamper(logger: Any, method_name: str, event_dict: Any) -> Any:
    event_dict["_abs_time"] = time.monotonic()
    return event_dict


def _relative_time_stamper(logger: Any, method_name: str, event_dict: Any) -> Any:
    abs_time = event_dict.pop("_abs_time", None) or time.monotonic()
    start = event_dict.pop("test_start_time", None) or _session_start_time
    elapsed = abs_time - start
    seconds = int(elapsed)
    milliseconds = int((elapsed - seconds) * 1000)
    event_dict["timestamp"] = f"{seconds:03d}.{milliseconds:03d}"
    return event_dict


# ── Default processor chain ──────────────────────────────────────

_BASE_PROCESSORS: list[Callable[[Any, str, Any], Any]] = [
    structlog.stdlib.filter_by_level,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.contextvars.merge_contextvars,
    _abs_time_stamper,
    _relative_time_stamper,
    structlog.processors.StackInfoRenderer(),
    _capture_processor,  # ← snapshot BEFORE the drop gate
    _life_check_processor,  # ← may DropEvent here
    structlog.dev.ConsoleRenderer(colors=False),
]


def configure_test_logging(*, extra_processors: list[Callable[[Any, str, Any], Any]] | None = None) -> None:
    """Reset structlog to test defaults, optionally injecting extra processors.

    Extra processors are inserted after ``add_log_level`` (position 3) so they
    have access to ``event``, ``logger``, and ``level`` fields, and execute
    before ``_capture_processor`` (so captured events include any mutations).
    """
    if extra_processors:
        idx = 3
        processors = _BASE_PROCESSORS[:idx] + extra_processors + _BASE_PROCESSORS[idx:]
    else:
        processors = _BASE_PROCESSORS

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


# ── Processor factories (vote-to-drop helpers) ───────────────────


def suppress_messages_containing(*substrings: str) -> Callable[[Any, str, Any], Any]:
    lowered = tuple(s.lower() for s in substrings)

    def _suppress(logger: Any, method_name: str, event_dict: Any) -> Any:
        msg = str(event_dict.get("event", "")).lower()
        if any(s in msg for s in lowered):
            event_dict[_LIFE_KEY] = event_dict.get(_LIFE_KEY, 0) - 1
        return event_dict

    return _suppress


def revive_messages_containing(*substrings: str) -> Callable[[Any, str, Any], Any]:
    lowered = tuple(s.lower() for s in substrings)

    def _revive(logger: Any, method_name: str, event_dict: Any) -> Any:
        msg = str(event_dict.get("event", "")).lower()
        if any(s in msg for s in lowered):
            event_dict[_LIFE_KEY] = event_dict.get(_LIFE_KEY, 0) + 1
        return event_dict

    return _revive


def suppress_messages_matching(pattern: str) -> Callable[[Any, str, Any], Any]:
    regex = re.compile(pattern)

    def _suppress(logger: Any, method_name: str, event_dict: Any) -> Any:
        msg = str(event_dict.get("event", ""))
        if regex.search(msg):
            event_dict[_LIFE_KEY] = event_dict.get(_LIFE_KEY, 0) - 1
        return event_dict

    return _suppress


def revive_messages_matching(pattern: str) -> Callable[[Any, str, Any], Any]:
    regex = re.compile(pattern)

    def _revive(logger: Any, method_name: str, event_dict: Any) -> Any:
        msg = str(event_dict.get("event", ""))
        if regex.search(msg):
            event_dict[_LIFE_KEY] = event_dict.get(_LIFE_KEY, 0) + 1
        return event_dict

    return _revive


# ── Apply initial configuration ──────────────────────────────────

configure_test_logging()

log = structlog.get_logger(__name__)

# Suppress noisy loggers globally.
logging.getLogger("asyncio").setLevel(logging.INFO)
logging.getLogger("aiosqlite").setLevel(logging.INFO)
logging.getLogger("pynixd.store.pool").setLevel(logging.INFO)


# ── Level management ──────────────────────────────────────────────


@contextmanager
def set_log_levels(levels: dict[str, int]):
    """Temporarily set logger levels, restoring them on exit."""
    saved = {}
    for name, level in levels.items():
        logger = logging.getLogger(name)
        saved[name] = logger.level
        logger.setLevel(level)
    try:
        yield
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


# ── Log file path helpers ────────────────────────────────────────


@pytest.fixture(scope="session")
def test_log_dir(request: pytest.FixtureRequest) -> Path:
    """Return the session-wide log directory."""
    return request.session.config.stash[_log_dir_key]


def get_log_file_path(log_dir: Path, item: Any) -> Path:
    """Generate a consistent log file path: log_dir/test_file::test_func.log"""
    file_stem = item.path.stem
    safe_name = item.name.replace("/", "_")
    return log_dir / f"{file_stem}::{safe_name}.log"


# ── File handler with relative timestamps ────────────────────────


class _TestRelativeTimeHandler(logging.FileHandler):
    """File handler that rewrites timestamps relative to test start."""

    _TS_LEN = 7

    def __init__(self, test_start: float, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._test_start = test_start

    def emit(self, record: logging.LogRecord) -> None:
        if record.created:
            elapsed = record.created - self._test_start
            seconds = int(elapsed)
            milliseconds = int((elapsed - seconds) * 1000)
            ts = f"{seconds:03d}.{milliseconds:03d}"
            if record.msg and len(record.msg) >= self._TS_LEN:
                record.msg = ts + record.msg[self._TS_LEN :]
        super().emit(record)


# ── Setup / teardown helpers ─────────────────────────────────────


_MAX_VALUES = 5


def _setup_test_logging(log_file: Path) -> tuple[logging.FileHandler, int]:
    """Attach a per-test log file and reset the structlog processor chain.

    Sets root logger to DEBUG so ``filter_by_level`` does not suppress
    events before ``_capture_processor``.  Returns (handler, old_level)
    so the caller can restore the level on teardown.
    """
    handler = _TestRelativeTimeHandler(time.time(), log_file)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)

    configure_test_logging()
    return handler, old_level


def _summarize(events: list[dict[str, Any]], *, write: Callable[[str], object] = print) -> None:
    """Print per-test log statistics.

    Args:
        events: Captured event dicts for this test.
        write: Output function (default ``print``).  The fixture writes to
            the per-test log file.
    """
    if not events:
        return

    by_source: Counter[tuple[str, str]] = Counter()
    by_operation: Counter[str] = Counter()
    for ed in events:
        by_source[(ed.get("logger", ""), ed.get("event", ""))] += 1
        if "operation" in ed:
            by_operation[ed["operation"]] += 1

    dropped = sum(1 for e in events if e.get(_LIFE_KEY, 0) < 0)

    write(f"[logs] {len(events)} events, {dropped} dropped ({len(by_source)} unique)")

    for (logger_, event), count in by_source.most_common(_MAX_VALUES):
        example = next(
            (e for e in events if e.get("logger") == logger_ and e.get("event") == event),
            {},
        )
        detail = {k: v for k, v in example.items() if k not in _SKIP_KEYS}
        detail = {k: (str(v)[:80] if isinstance(v, str) else v) for k, v in detail.items()}
        short_logger = logger_.rsplit(".", 1)[-1] if "." in logger_ else logger_
        write(f"  {short_logger}:{event} x{count}  {dict(detail)}")

    if by_operation:
        ops = dict(by_operation.most_common(_MAX_VALUES))
        write(f"  [ops] {ops}")


# ── Single autouse fixture (replaces test_log_file + _reset_structlog) ─


@pytest.fixture(autouse=True)
def test_logging(request: pytest.FixtureRequest, test_log_dir: Path):
    """Per-test log capture, file output, and statistics.

    Swaps in a fresh event list, opens a per-test log file, resets the
    structlog processor chain, and on teardown prints log statistics.
    """
    events: list[dict[str, Any]] = []
    global _captured_events
    _captured_events = events

    log_file = get_log_file_path(test_log_dir, request.node)
    handler, old_log_level = _setup_test_logging(log_file)

    structlog.contextvars.bind_contextvars(test_start_time=time.monotonic())

    yield

    _captured_events = []
    logging.getLogger().setLevel(old_log_level)
    handler.close()

    # Append per-test log statistics to the log file.
    with open(log_file, "a") as f:
        _summarize(events, write=lambda s: f.write(s + "\n"))
