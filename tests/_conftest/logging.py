"""Structured logging configuration and per-test log file fixtures.

Provides ``configure_test_logging()`` to override structlog processors per test,
and an autouse fixture that resets to defaults between every test run.
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
import structlog
from structlog import DropEvent

from tests._conftest.constants import _log_dir_key

# ── Life counter: cooperative vote-to-drop system ────────────────
# Processors can decrement (suppress) or increment (revive) this
# counter on event_dict.  _life_check_processor at the end of the
# chain drops events whose counter is negative.

_LIFE_KEY = "_pynixd_life"

# Module-level counter aggregating dropped events by (operation, logger, event).
# Cleared per test in _reset_structlog.  asyncio tasks in the same event loop
# are cooperative so no lock needed.
_dropped_counter: Counter[tuple[str, str, str]] = Counter()


def _life_check_processor(logger: Any, method_name: str, event_dict: Any) -> Any:
    """Final gate — drop events whose life counter is negative.

    Pops ``_LIFE_KEY`` so it doesn't leak into rendered output.
    Aggregates dropped events by (operation, logger, event) for summaries.
    """
    life = event_dict.pop(_LIFE_KEY, 0)
    if life < 0:
        key = (
            method_name,
            str(event_dict.get("logger", "")),
            str(event_dict.get("event", "")),
        )
        _dropped_counter[key] += 1
        raise DropEvent()  # noqa: RSE102
    return event_dict


# ── Time stampers (used by the processor chain below) ─────────────

_session_start_time = time.monotonic()


def _abs_time_stamper(logger: Any, method_name: str, event_dict: Any) -> Any:
    """Store absolute monotonic time for per-handler formatting."""
    event_dict["_abs_time"] = time.monotonic()
    return event_dict


def _relative_time_stamper(logger: Any, method_name: str, event_dict: Any) -> Any:
    """Compute timestamp relative to test_start_time or session start."""
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
    _life_check_processor,
    structlog.dev.ConsoleRenderer(colors=False),
]


def configure_test_logging(*, extra_processors: list[Callable[[Any, str, Any], Any]] | None = None) -> None:
    """Reset structlog to test defaults, optionally injecting extra processors.

    ``extra_processors`` are inserted after ``add_log_level`` (position 3)
    so they have access to ``event``, ``logger``, and ``level`` fields.

    Example::

        configure_test_logging(extra_processors=[suppress_messages_containing("noise")])
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


# ── Common processor factories ────────────────────────────────────


_KW = _LIFE_KEY


def suppress_messages_containing(*substrings: str) -> Callable[[Any, str, Any], Any]:
    """Return a processor that decrements the life counter for matching messages.

    Supports ``revive_messages_containing`` to undo.  Case-insensitive.

    Example::

        configure_test_logging(extra_processors=[suppress_messages_containing("noise", "debug_")])
    """
    lowered = tuple(s.lower() for s in substrings)

    def _suppress(logger: Any, method_name: str, event_dict: Any) -> Any:
        msg = str(event_dict.get("event", "")).lower()
        if any(s in msg for s in lowered):
            event_dict[_KW] = event_dict.get(_KW, 0) - 1
        return event_dict

    return _suppress


def revive_messages_containing(*substrings: str) -> Callable[[Any, str, Any], Any]:
    """Return a processor that increments the life counter for matching messages.

    Counterpart to ``suppress_messages_containing``.  Case-insensitive.

    Example::

        configure_test_logging(
            extra_processors=[
                suppress_messages_containing("noise"),
                revive_messages_containing("important"),
            ]
        )
    """
    lowered = tuple(s.lower() for s in substrings)

    def _revive(logger: Any, method_name: str, event_dict: Any) -> Any:
        msg = str(event_dict.get("event", "")).lower()
        if any(s in msg for s in lowered):
            event_dict[_KW] = event_dict.get(_KW, 0) + 1
        return event_dict

    return _revive


def suppress_messages_matching(pattern: str) -> Callable[[Any, str, Any], Any]:
    """Return a processor that decrements the life counter for events matching a regex.

    Supports ``revive_messages_matching`` to undo.

    Example::

        configure_test_logging(extra_processors=[suppress_messages_matching(r"worker-\\d+")])
    """
    regex = re.compile(pattern)

    def _suppress(logger: Any, method_name: str, event_dict: Any) -> Any:
        msg = str(event_dict.get("event", ""))
        if regex.search(msg):
            event_dict[_KW] = event_dict.get(_KW, 0) - 1
        return event_dict

    return _suppress


def revive_messages_matching(pattern: str) -> Callable[[Any, str, Any], Any]:
    """Return a processor that increments the life counter for events matching a regex.

    Counterpart to ``suppress_messages_matching``.

    Example::

        configure_test_logging(
            extra_processors=[
                suppress_messages_matching(r"health-"),
                revive_messages_matching(r"health-critical"),
            ]
        )
    """
    regex = re.compile(pattern)

    def _revive(logger: Any, method_name: str, event_dict: Any) -> Any:
        msg = str(event_dict.get("event", ""))
        if regex.search(msg):
            event_dict[_KW] = event_dict.get(_KW, 0) + 1
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


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def test_log_dir(request: pytest.FixtureRequest) -> Path:
    """Return the session-wide log directory."""
    return request.session.config.stash[_log_dir_key]


def get_log_file_path(log_dir: Path, item: Any) -> Path:
    """Generate a consistent log file path: log_dir/test_file::test_func.log"""
    file_stem = item.path.stem
    safe_name = item.name.replace("/", "_")
    return log_dir / f"{file_stem}::{safe_name}.log"


@pytest.fixture(autouse=True)
def test_log_file(request: pytest.FixtureRequest, test_log_dir: Path):
    """Redirect all structlog output for this test to its own log file."""
    log_file = get_log_file_path(test_log_dir, request.node)

    structlog.contextvars.bind_contextvars(test_start_time=time.monotonic())

    test_start = time.time()
    handler = _TestRelativeTimeHandler(test_start, log_file)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    old_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)

    yield log_file

    root_logger.removeHandler(handler)
    root_logger.setLevel(old_level)
    handler.close()


@pytest.fixture(autouse=True)
def _reset_structlog(test_log_file: Path):
    """Reset structlog to defaults before each test.

    Runs after ``test_log_file`` so the handler setup happens under the
    initial (module-level) configuration, then resets so any per-test
    mutation via ``configure_test_logging()`` is cleaned up for the next test.

    On teardown, logs a summary of any events dropped by ``_life_check_processor``.
    """
    _dropped_counter.clear()
    configure_test_logging()
    yield
    if _dropped_counter:
        by_operation: dict[str, int] = {}
        by_logger: dict[str, int] = {}
        by_event: dict[str, int] = {}
        for (op, lg, ev), cnt in _dropped_counter.items():
            by_operation[op] = by_operation.get(op, 0) + cnt
            by_logger[lg] = by_logger.get(lg, 0) + cnt
            by_event[ev] = by_event.get(ev, 0) + cnt
        log.warning(
            "DROPPED_EVENTS",
            total=sum(_dropped_counter.values()),
            by_operation=by_operation,
            by_logger=by_logger,
            by_event=by_event,
        )


class _TestRelativeTimeHandler(logging.FileHandler):
    """File handler that recomputes timestamps relative to test start."""

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
