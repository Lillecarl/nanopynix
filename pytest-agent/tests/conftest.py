"""Harness for testing pytest-agent with pytest.

Recursive by design: most of what pytest-agent does only exists inside a real
pytest session, so these tests run *inner* pytest sessions (via
``pytest.Pytester``) and inner ``pytest-agent`` CLI invocations against
throwaway project directories, then read back what those wrote.

Two things make that honest rather than accidental:

* ``_clean_agent_env`` -- inner processes inherit this process's environment,
  and this repo's own dev environment is itself an AI-agent harness session
  (``CLAUDECODE`` is genuinely set here). Left alone, every inner run would
  auto-activate agent mode and every "is it off by default?" test would pass
  for the wrong reason. So every harness env var is cleared for all tests,
  and a test that wants one sets it explicitly.
* ``agent_plugin_cli_args`` -- an inner run has to load the plugin the same
  way the ambient environment does, which differs between a real install and
  a bare-PYTHONPATH checkout.

The pipe guard needs no bootstrapping escape hatch: it inspects the *inner*
process's own stdout, which Pytester points at a file, so an outer run being
piped anywhere is irrelevant to it.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pytest_agent._entry_points import plugin_registered_via_entry_points
from pytest_agent._harness_detect import HARNESS_ENV_VARS

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

pytest_plugins = ["pytester"]

# Generous: these wait on a real subprocess reaching a known state, and the
# failure mode of waiting too long is a slow test, not a wrong one.
WAIT_TIMEOUT = 30.0

_SRC = Path(__file__).resolve().parents[1] / "src"

# A temp root of our own, rather than the shared /tmp/pytest-of-$USER.
#
# pytest keeps the newest N numbered directories under a temp root and deletes
# the rest at process exit, so any two suites sharing one root are pruning
# each other's directories by number. This suite was killed mid-run exactly
# that way while the surrounding project's suite ran alongside it: 93 tests
# failed at once with FileNotFoundError on the base temp directory, because
# something had removed the live session's own basetemp out from under it.
#
# Set at import time because getbasetemp() is lazy -- it resolves on the first
# tmp_path use, which is inside a test, long after this module is imported.
# setdefault so an explicit outer value still wins.
_SELF_TEST_TEMPROOT = Path(tempfile.gettempdir()) / "pytest-agent-selftest"
_SELF_TEST_TEMPROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_SELF_TEST_TEMPROOT))

# Everything pytest-agent reads from the environment. Cleared for every test
# so that inner processes start from a known-empty configuration.
_AGENT_ENV_VARS = (
    "PYTEST_AGENT",
    "PYTEST_AGENT_NO_AUTODETECT",
    "PYTEST_AGENT_DIR",
    "PYTEST_AGENT_HEARTBEAT",
    "PYTEST_AGENT_STUCK_AFTER",
    "PYTEST_AGENT_STATUS_INTERVAL",
    "PYTEST_AGENT_KEEP_RUNS",
    "PYTEST_AGENT_MAX_SUMMARY_LINES",
    "PYTEST_AGENT_ALLOW_PIPE",
)


@pytest.fixture(autouse=True)
def _clean_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Give every inner process a known-empty pytest-agent environment.

    PYTHONPATH is set explicitly because pyproject.toml's ``pythonpath`` ini
    option only affects sys.path for *this* pytest run -- a child process
    needs the real environment variable to find pytest_agent uninstalled.
    """
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(_SRC), *([existing] if existing else [])]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(parts))
    for name in (*HARNESS_ENV_VARS, *_AGENT_ENV_VARS):
        monkeypatch.delenv(name, raising=False)


def agent_plugin_cli_args() -> list[str]:
    """Extra argv needed for a subprocess pytest run to load pytest_agent.plugin.

    Empty when this process's own environment already provides it via a
    pytest11 entry point (a real install -- the subprocess inherits the same
    site-packages/PYTHONPATH), since passing `-p pytest_agent.plugin` on top
    of an already-active entry point crashes (pluggy refuses to register the
    same plugin module under two different names). Otherwise
    `-p pytest_agent.plugin`, needed when pytest-agent is merely on
    PYTHONPATH with no install metadata at all.
    """
    return [] if plugin_registered_via_entry_points() else ["-p", "pytest_agent.plugin"]


def run_cli(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the pytest-agent CLI in *cwd*, the way a user or agent would.

    Through ``python -m pytest_agent`` rather than the installed console
    script, so the test doesn't depend on whether this checkout happens to be
    installed; and through a real subprocess rather than calling ``main()``
    in-process, so exit codes, argv dispatch and the cwd-relative paths the
    CLI prints are all the real thing.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest_agent", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # callers assert on returncode themselves
    )


@contextlib.contextmanager
def running_pytest(project: Path, *args: str) -> Generator[subprocess.Popen[str]]:
    """A live `pytest --agent` subprocess in *project*, killed on the way out.

    For the behaviours that only exist while a run is still going: being
    signalled, being hung, or being concurrent with another run. The kill in
    the finally matters -- these tests start runs that would otherwise sleep
    for minutes, and an assertion failing before the signal is sent must not
    leave one behind.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", *agent_plugin_cli_args(), "--agent", *args],
        cwd=project,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=WAIT_TIMEOUT)


def wait_until(predicate: Callable[[], bool], description: str) -> None:
    """Block until *predicate* holds, or fail the test saying what it waited for."""
    deadline = time.monotonic() + WAIT_TIMEOUT
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {WAIT_TIMEOUT}s waiting for {description}")
