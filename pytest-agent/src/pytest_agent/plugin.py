from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

import pytest
from _pytest.config import (
    create_terminal_writer,  # type: ignore[reportPrivateImportUsage] -- no public equivalent; see _silence_terminal_reporter
)

from pytest_agent._harness_detect import detect_agent_harness
from pytest_agent._pipe_guard import find_banned_pipe_reader
from pytest_agent._runtime import AgentRuntime
from pytest_agent._terminal import RealTerminal

_RUNTIME_PLUGIN_NAME = "pytest-agent-runtime"

# Set by pytest_addoption, read by pytest_configure: which harness env var (if
# any) caused --agent's default to turn on by itself, so the startup banner
# can explain why agent mode is active when nobody passed --agent explicitly.
_autodetected_via: str | None = None

def _make_real_terminal() -> RealTerminal | None:
    try:
        return RealTerminal()
    except OSError:
        return None


# Must be created at plugin import time, before pytest's capture manager
# starts redirecting fd 1 -- see RealTerminal's docstring. Plugin modules are
# imported (via entry points or `-p`) well before pytest_load_initial_conftests
# starts global capturing, so this ordering holds.
_REAL_TERMINAL: RealTerminal | None = _make_real_terminal()


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _agent_default() -> bool:
    global _autodetected_via
    if _env_flag("PYTEST_AGENT"):
        return True
    if _env_flag("PYTEST_AGENT_NO_AUTODETECT"):
        return False
    harness = detect_agent_harness()
    if harness is None:
        return False
    _autodetected_via = harness
    return True


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("agent", "pytest-agent: AI-agent-friendly test output")
    group.addoption(
        "--agent",
        action="store_true",
        default=_agent_default(),
        help=(
            "Minimal CLI output (a periodic progress line only); full per-test detail "
            "written to --agent-dir. Turns on by itself if a known AI coding-agent "
            "harness env var is set (PYTEST_AGENT_NO_AUTODETECT=1 disables that)."
        ),
    )
    group.addoption(
        "--agent-dir",
        default=os.environ.get("PYTEST_AGENT_DIR", ".pytest-agent"),
        help="Directory for agent-mode run detail, relative to rootdir (default: %(default)s).",
    )
    group.addoption(
        "--agent-heartbeat",
        type=float,
        default=float(os.environ.get("PYTEST_AGENT_HEARTBEAT", "10")),
        help="Seconds between progress lines while tests run (default: %(default)s).",
    )
    group.addoption(
        "--agent-allow-pipe",
        action="store_true",
        default=_env_flag("PYTEST_AGENT_ALLOW_PIPE"),
        help="Skip the head/tail/grep/sed/awk piped-stdout guard.",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> int | None:
    if config.getoption("agent_allow_pipe"):
        return None
    reader = find_banned_pipe_reader()
    if reader is None:
        return None
    sys.stderr.write(
        f"pytest-agent: refusing to run -- stdout is piped directly into '{reader}', "
        "which truncates pytest's output and can hide the real failure.\n"
        "Run pytest without piping into head/tail/grep/sed/awk. Use --agent mode "
        "instead: it writes full per-test detail to disk and only prints a short "
        "periodic progress line, so there is nothing left that needs truncating.\n"
        "Pass --agent-allow-pipe (or set PYTEST_AGENT_ALLOW_PIPE=1) if this is intentional.\n"
    )
    return 2


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    if not config.getoption("agent"):
        return

    _silence_terminal_reporter(config)

    agent_dir = cast("str", config.getoption("agent_dir"))
    root = Path(agent_dir)
    if not root.is_absolute():
        root = config.rootpath / root

    # _agent_default() (and its _autodetected_via side effect) runs
    # unconditionally at parser-setup time to compute --agent's default, even
    # when the user passes --agent explicitly on the command line. Only show
    # the "auto-activated" banner when --agent's value actually came from
    # that default, not from an explicit flag.
    explicit_agent_flag = "--agent" in config.invocation_params.args
    autodetected_via = None if explicit_agent_flag else _autodetected_via

    heartbeat_interval = cast("float", config.getoption("agent_heartbeat"))
    runtime = AgentRuntime(
        config,
        root=root,
        heartbeat_interval=heartbeat_interval,
        terminal=_REAL_TERMINAL,
        autodetected_via=autodetected_via,
    )
    config.pluginmanager.register(runtime, _RUNTIME_PLUGIN_NAME)


def _silence_terminal_reporter(config: pytest.Config) -> None:
    """Make the builtin terminal reporter print nothing, without fully
    unregistering it.

    Fully unregistering it (config.pluginmanager.unregister(...)) was tried
    first and broke pytest's own assertion-rewrite comparison output:
    Config.get_terminal_writer() asserts the "terminalreporter" plugin is
    still registered, and pytest_assertrepr_compare calls that internally to
    get a highlighter for every failing comparison. Instead, the reporter
    stays registered (so that internal lookup keeps succeeding) but its
    output file is swapped for os.devnull, so every dot/PASSED-line/summary
    it would normally print is silently discarded.
    """
    terminal_reporter = config.pluginmanager.get_plugin("terminalreporter")
    if terminal_reporter is None:
        return
    devnull = Path(os.devnull).open("w", encoding="utf-8")
    terminal_reporter._tw = create_terminal_writer(config, devnull)  # type: ignore[reportPrivateUsage] -- see docstring above


def pytest_unconfigure(config: pytest.Config) -> None:
    runtime = config.pluginmanager.get_plugin(_RUNTIME_PLUGIN_NAME)
    if runtime is not None:
        config.pluginmanager.unregister(runtime)
