"""Test helpers for pynixd functional tests."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import shlex
import stat
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import structlog
from environs import env

# Structlog configuration
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(colors=False),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

log = structlog.get_logger(__name__)

logging.getLogger("asyncio").setLevel(logging.INFO)
logging.getLogger("aiosqlite").setLevel(logging.INFO)
logging.getLogger("pynixd.store.pool").setLevel(logging.INFO)


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


NIX_BIN = env.str("NIX_BIN", "nix")
LIX_BIN = env.str("LIX_BIN", "nix")

STORE_PREFIX = Path("/tmp/pynixd-stores")

_log_dir_key = pytest.StashKey[Path]()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Create session-wide log directory and print its path."""
    run_id = str(int(time.time()))
    log_dir = Path(f"/tmp/pynixd-logs/{run_id}")
    log_dir.mkdir(parents=True, exist_ok=True)
    session.config.stash[_log_dir_key] = log_dir

    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr:
        tr.write_line(f"\nIMPORTANT: Test run logs: {log_dir}")


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Print log directory path at the end of the test run."""
    log_dir = config.stash.get(_log_dir_key, None)
    if log_dir:
        terminalreporter.write_line(f"\nIMPORTANT: Test run logs: {log_dir}")


@pytest.fixture(scope="session")
def test_log_dir(request: pytest.FixtureRequest) -> Path:
    """Return the session-wide log directory."""
    return request.session.config.stash[_log_dir_key]


@pytest.fixture(autouse=True)
def test_log_file(request: pytest.FixtureRequest, test_log_dir: Path):
    """Redirect all structlog output for this test to its own log file."""
    node = request.node
    safe_name = node.name.replace("/", "_")
    log_file = test_log_dir / f"{safe_name}.log"

    handler = logging.FileHandler(log_file)
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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    """
    Write captured output and failure details to log file, suppress console display.
    """
    outcome = yield
    report = outcome.get_result()

    if report.when == "call" and report.failed:
        log_dir = item.config.stash.get(_log_dir_key, None)
        if log_dir:
            safe_name = item.name.replace("/", "_")
            log_file = log_dir / f"{safe_name}.log"
            with open(log_file, "a") as f:
                if report.longrepr:
                    f.write("\n--- Failure details ---\n")
                    f.write(str(report.longrepr))
                if report.capstdout:
                    f.write("\n--- Captured stdout ---\n")
                    f.write(report.capstdout)
                if report.capstderr:
                    f.write("\n--- Captured stderr ---\n")
                    f.write(report.capstderr)

            # Replace longrepr with short message for console
            report.longrepr = f"FAILED (see log file: {log_file / f'{safe_name}.log'})"


def rmtree_robust(path: str | Path) -> None:
    """Recursively remove a directory, unsetting read-only bits as needed."""
    path = Path(path)
    if not path.exists():
        return

    def handle_errors(func, path, _excinfo):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(path)
        except Exception:
            pass

    import shutil

    shutil.rmtree(path, onerror=handle_errors)


def rmtree_robust_glob(pattern: str) -> None:
    """Remove all directories matching a glob pattern."""
    for path in glob.glob(pattern):
        rmtree_robust(path)


@pytest.fixture(autouse=True)
def cleanup_stores():
    """Remove any leftover test stores before each test."""
    rmtree_robust_glob(f"{STORE_PREFIX}/*")
    yield


async def run_captured(cmd: list[str], **kwargs) -> tuple[int, str, str]:
    """Run a command and capture stdout/stderr."""
    run_env = os.environ.copy()
    run_env.update(kwargs.pop("env", {}))
    if "NIX_SSHOPTS" not in run_env:
        run_env["NIX_SSHOPTS"] = (
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        )

    log.debug("run_captured", cmd=shlex.join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=run_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode(), stderr.decode()


async def run_logged(cmd: list[str], **kwargs) -> int:
    """Run a command, streaming stdout/stderr through structlog in real-time."""
    run_env = os.environ.copy()
    run_env.update(kwargs.pop("env", {}))
    if "NIX_SSHOPTS" not in run_env:
        run_env["NIX_SSHOPTS"] = (
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        )

    log.debug("run_logged", cmd=shlex.join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=run_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )

    async def stream(name: str, pipe):
        while True:
            line = await pipe.readline()
            if not line:
                break
            log.info(name, message=line.decode().rstrip())

    await asyncio.gather(
        stream("stdout", proc.stdout),
        stream("stderr", proc.stderr),
    )
    return proc.returncode or 0
