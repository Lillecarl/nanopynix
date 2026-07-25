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

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pytest_agent._entry_points import plugin_registered_via_entry_points
from pytest_agent._harness_detect import HARNESS_ENV_VARS

if TYPE_CHECKING:
    from collections.abc import Sequence

pytest_plugins = ["pytester"]

_SRC = Path(__file__).resolve().parents[1] / "src"

# Everything pytest-agent reads from the environment. Cleared for every test
# so that inner processes start from a known-empty configuration.
_AGENT_ENV_VARS = (
    "PYTEST_AGENT",
    "PYTEST_AGENT_NO_AUTODETECT",
    "PYTEST_AGENT_DIR",
    "PYTEST_AGENT_HEARTBEAT",
    "PYTEST_AGENT_STUCK_AFTER",
    "PYTEST_AGENT_KEEP_RUNS",
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
