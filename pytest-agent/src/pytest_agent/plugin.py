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
from pytest_agent._history import next_run_dir, validate_run_label
from pytest_agent._notes import agent_notes as agent_notes
from pytest_agent._notes import pop_runtime, push_runtime
from pytest_agent._pipe_guard import find_banned_pipe_reader, zero_detail_mode
from pytest_agent._profile import profile as profile
from pytest_agent._runtime import RUNTIME_PLUGIN_NAME, AgentRuntime
from pytest_agent._terminal import RealTerminal

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
    global _autodetected_via  # noqa: PLW0603 -- one-shot record of which harness env var triggered autodetection, set once during pytest startup
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
        "--agent-label",
        default=os.environ.get("PYTEST_AGENT_LABEL") or None,
        metavar="NAME",
        help=(
            "Name this run, so later queries can find it by name instead of by "
            "number: `pytest --agent-label nightly tests` then "
            "`pytest-agent last-failures --run nightly`. Labeled runs get their own "
            "retention budget, so a long run started in the background survives the "
            "focused runs done while waiting for it. Letters, digits, '.', '_' and "
            "'-'; not all digits (that is a run number)."
        ),
    )
    group.addoption(
        "--agent-heartbeat",
        type=float,
        default=float(os.environ.get("PYTEST_AGENT_HEARTBEAT", "10")),
        help="Seconds between progress lines while tests run (default: %(default)s).",
    )
    group.addoption(
        "--agent-stuck-after",
        type=float,
        default=float(os.environ.get("PYTEST_AGENT_STUCK_AFTER", "300")),
        help=(
            "After a single test has run this many seconds, dump every thread's "
            "stack to <test>.stuck.txt beside where its log will go, and print the "
            "path (repeats up to 5 times per test; 0 disables). The default sits "
            "below the `timeout 500 pytest` an agent typically uses, so a hung run "
            "leaves its stack behind before the kill arrives (default: %(default)s)."
        ),
    )
    group.addoption(
        "--agent-keep-runs",
        type=int,
        default=int(os.environ.get("PYTEST_AGENT_KEEP_RUNS", "20")),
        help=(
            "Keep only the newest N runs-* directories under --agent-dir, deleting "
            "older ones after each run (clamped to at least 1: the just-finished run "
            "is never pruned). history.jsonl entries are kept forever regardless "
            "(default: %(default)s)."
        ),
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
    if zero_detail_mode(config) is not None:
        return None
    reader = find_banned_pipe_reader()
    if reader is None:
        return None
    # Deliberately no mention of --agent-allow-pipe here. The flag exists
    # (documented in the README) for the rare human who means it, but naming
    # it in the refusal an agent is reading turns "stop truncating" into
    # "add this flag and carry on truncating" -- the opposite of the point.
    sys.stderr.write(
        f"pytest-agent: refusing to run -- stdout is piped directly into '{reader}', "
        "which truncates pytest's output and can hide the real failure.\n"
        "This guard is independent of agent mode: it applies to every pytest run in "
        "this environment, and PYTEST_AGENT_NO_AUTODETECT=1 does not turn it off.\n"
        "Re-run without the pipe. Agent mode writes full per-test detail to disk, and "
        "`pytest-agent last-failures|show|digest` answer the question you were "
        "reaching for head/tail/grep to answer.\n",
    )
    return 2


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    if not config.getoption("agent"):
        return

    # A listing-only run (--collect-only, --fixtures, ...) has no per-test
    # detail to record, and its listing *is* the answer being asked for --
    # silencing the terminal reporter would leave the caller with nothing at
    # all, and claiming a runs-NNNN directory for it would be pure litter.
    # Agent mode is a no-op here, so plain pytest behavior stands.
    if zero_detail_mode(config) is not None:
        return

    # Before claiming a run directory, so a rejected label doesn't leave an
    # empty runs-NNNN behind; and here rather than in pytest_addoption, so a
    # --collect-only run that never records anything isn't refused over the
    # spelling of a label it was never going to use.
    label = cast("str | None", config.getoption("agent_label"))
    if label is not None:
        try:
            validate_run_label(label)
        except ValueError as error:
            raise pytest.UsageError(f"--agent-label: {error}") from None

    _silence_terminal_reporter(config)

    agent_dir = cast("str", config.getoption("agent_dir"))
    top_root = Path(agent_dir)
    if not top_root.is_absolute():
        top_root = config.rootpath / top_root
    run_number, root = next_run_dir(top_root)

    # _agent_default() (and its _autodetected_via side effect) runs
    # unconditionally at parser-setup time to compute --agent's default, even
    # when the user passes --agent explicitly on the command line. Only show
    # the "auto-activated" banner when --agent's value actually came from
    # that default, not from an explicit flag.
    explicit_agent_flag = "--agent" in config.invocation_params.args
    autodetected_via = None if explicit_agent_flag else _autodetected_via

    heartbeat_interval = cast("float", config.getoption("agent_heartbeat"))
    keep_runs = cast("int", config.getoption("agent_keep_runs"))
    stuck_after = cast("float", config.getoption("agent_stuck_after"))
    runtime = AgentRuntime(
        config,
        root=root,
        top_root=top_root,
        run_number=run_number,
        keep_runs=keep_runs,
        heartbeat_interval=heartbeat_interval,
        stuck_after=stuck_after,
        terminal=_REAL_TERMINAL,
        label=label,
        autodetected_via=autodetected_via,
    )
    config.pluginmanager.register(runtime, RUNTIME_PLUGIN_NAME)
    # So pytest_agent.note() can find this session without a fixture to carry
    # it -- the whole point of that entry point is being callable from inside
    # the code under test.
    push_runtime(runtime)


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
    terminal_reporter._tw = create_terminal_writer(config, devnull)  # type: ignore[reportPrivateUsage] -- see docstring above  # noqa: SLF001


def pytest_unconfigure(config: pytest.Config) -> None:
    runtime = config.pluginmanager.get_plugin(RUNTIME_PLUGIN_NAME)
    if runtime is not None:
        pop_runtime(cast("AgentRuntime", runtime))
        config.pluginmanager.unregister(runtime)
