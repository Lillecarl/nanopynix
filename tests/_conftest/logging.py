"""Structured logging configuration and per-test log file fixtures."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import pytest
import structlog

from tests._conftest.constants import _log_dir_key

# ── Structlog setup ───────────────────────────────────────────────

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


structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.contextvars.merge_contextvars,
        _abs_time_stamper,
        _relative_time_stamper,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(colors=False),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

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
