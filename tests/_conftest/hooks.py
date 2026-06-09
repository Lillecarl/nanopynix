"""Pytest hooks: collection, session lifecycle, and reporting."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import structlog

from tests._conftest.constants import _covered_features_key, _log_dir_key
from tests._conftest.logging import get_log_file_path
from tests._conftest.subsumption import _sort_by_subsumption
from tests.test_features import TestFeatures

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator, Sequence

log = structlog.get_logger(__name__)


# ── Collection filtering ──────────────────────────────────────────


def pytest_ignore_collect(collection_path: Path) -> bool:
    """Skip integration test directories during normal collection."""
    return "nar_integration" in str(collection_path) or "drv_integration" in str(collection_path)


# ── Session lifecycle ─────────────────────────────────────────────


def pytest_sessionstart(session: pytest.Session) -> None:
    """Create session-wide log directory and clean up leftovers."""
    run_id = str(int(time.time()))
    log_dir = Path(f"/tmp/pynixd-logs/{run_id}")
    log_dir.mkdir(parents=True, exist_ok=True)
    session.config.stash[_log_dir_key] = log_dir

    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr:
        tr.write_line(f"\nIMPORTANT: Test run logs: {log_dir}")

    from tests._conftest.helpers import rmtree_robust_glob

    rmtree_robust_glob("/tmp/pynixd-test-*")
    rmtree_robust_glob("/tmp/pytest-of-lillecarl/*")


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Print log directory path at the end of the test run."""
    log_dir = config.stash.get(_log_dir_key, None)
    if log_dir:
        terminalreporter.write_line(f"\nIMPORTANT: Test run logs: {log_dir}")


# ── Collection / modification ─────────────────────────────────────


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]):
    """Wrap async tests in asyncio.timeout, sort by subsumption, handle Lix skips."""
    from tests._conftest.config import CLIENT_BIN, LIX_BIN, NIX_BIN

    # Sort by descending covers-popcount so broad tests run first.
    if not config.getoption("no_test_subsumption"):
        _sort_by_subsumption(items)

    # Resolve timeout value.
    try:
        default_timeout = config.getvalue("timeout")
    except (AttributeError, ValueError):
        default_timeout = None
    if default_timeout is None:
        default_timeout = os.environ.get("PYTEST_TIMEOUT")
    if default_timeout is None:
        default_timeout = config.getini("timeout")
    try:
        default_timeout = float(default_timeout) if default_timeout else 120.0
    except ValueError:
        default_timeout = 120.0

    for item in items:
        if (
            isinstance(item, pytest.Function)
            and asyncio.iscoroutinefunction(item.obj)
            and not getattr(item.obj, "_pynixd_timeout_wrapped", False)
        ):
            item.obj = _wrap_with_asyncio_timeout(item, default_timeout)
            item.obj._pynixd_timeout_wrapped = True  # type: ignore[reportAttributeAccessIssue]

    # Lix: skip CA/dynamic tests.
    client_bin = config.getoption("client_bin", "nix")
    local_bin = config.getoption("local_bin", "nix")
    builder_bin = config.getoption("builder_bin", "nix")
    if "lix" in (client_bin, local_bin, builder_bin):
        for item in items:
            if item.get_closest_marker("ca_derivations"):
                item.add_marker(pytest.mark.skip(reason="Not supported with Lix"))


def _wrap_with_asyncio_timeout(item: pytest.Function, default_timeout: float):
    """Wrap an async test function with asyncio.timeout that triggers before pytest-timeout."""
    original_func = item.obj

    @functools.wraps(original_func)
    async def wrapped(*args, **kwargs):
        timeout_mark = item.get_closest_marker("timeout")
        seconds = float(timeout_mark.args[0] if timeout_mark else default_timeout)

        if seconds <= 0:
            return await original_func(*args, **kwargs)

        timeout_val = max(1.0, seconds - 5.0)

        try:
            async with asyncio.timeout(timeout_val):
                return await original_func(*args, **kwargs)
        except TimeoutError:
            log.exception(
                "test_timeout_triggered",
                test=item.nodeid,
                timeout=seconds,
                effective=timeout_val,
            )
            raise

    return wrapped


# ── Test reporting ────────────────────────────────────────────────


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    """Record covered features for subsumption and write failure details to log file."""
    outcome = yield
    report = outcome.get_result()

    if report.when == "call":
        if report.passed and not item.config.getoption("no_test_subsumption"):
            marker = item.get_closest_marker("covers")
            if marker is not None and marker.args:
                features: TestFeatures = marker.args[0]
                covered = item.config.stash.get(_covered_features_key, TestFeatures(0))
                item.config.stash[_covered_features_key] = covered | features

        if report.failed:
            log_dir = item.config.stash.get(_log_dir_key, None)
            if log_dir:
                log_file = get_log_file_path(log_dir, item)
                with log_file.open("a") as f:
                    if report.longrepr:
                        f.write("\n--- Failure details ---\n")
                        f.write(str(report.longrepr))
                    if report.capstdout:
                        f.write("\n--- Captured stdout ---\n")
                        f.write(report.capstdout)
                    if report.capstderr:
                        f.write("\n--- Captured stderr ---\n")
                        f.write(report.capstderr)
                report.longrepr = f"FAILED (see log file: {log_file})"
