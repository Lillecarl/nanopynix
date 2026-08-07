from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from _pytest.config import (
    create_terminal_writer,  # type: ignore[reportPrivateImportUsage] -- no public equivalent; see _silence_terminal_reporter
)

from pytest_agent._harness_detect import detect_agent_harness
from pytest_agent._history import next_run_dir, validate_run_label
from pytest_agent._notes import agent_notes as agent_notes, pop_runtime, push_runtime
from pytest_agent._pipe_guard import find_banned_pipe_reader, zero_detail_mode
from pytest_agent._profile import profile as profile
from pytest_agent._runtime import (
    DEFAULT_MAX_TERMINAL_SUMMARY_LINES,
    DEFAULT_STATUS_INTERVAL,
    RUNTIME_PLUGIN_NAME,
    TERMINAL_LOG_NAME,
    AgentRuntime,
)
from pytest_agent._terminal import RealTerminal

if TYPE_CHECKING:
    from typing import TextIO

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


def _xdist_role(config: pytest.Config) -> str:
    """``"worker"``, ``"controller"``, or ``""`` when xdist is not distributing.

    ``workerinput`` is the attribute xdist sets on a worker's config and
    nothing else does; it is the documented way to tell the two apart.
    """
    if hasattr(config, "workerinput"):
        return "worker"
    if getattr(config.option, "dist", "no") not in {"no", None}:
        return "controller"
    return ""


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
        help=("Seconds between progress lines while tests run; 0 prints none (default: %(default)s)."),
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
        "--agent-status-interval",
        type=float,
        default=float(os.environ.get("PYTEST_AGENT_STATUS_INTERVAL", str(DEFAULT_STATUS_INTERVAL))),
        help=(
            "Seconds between rewrites of the run's status.json, which names the "
            "test running now and how long it has been running. This is what "
            "`pytest-agent watch` reads to report a stuck test from another "
            "process; 0 writes none (default: %(default)s)."
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
        "--agent-max-summary-lines",
        type=int,
        default=int(os.environ.get("PYTEST_AGENT_MAX_SUMMARY_LINES", str(DEFAULT_MAX_TERMINAL_SUMMARY_LINES))),
        metavar="N",
        help=(
            "Terminal lines an end-of-run report from another plugin (a `--cov` "
            "table, `--durations`) may take inline. A longer one is pointed at "
            "rather than shown in part, since half a table reads like a whole "
            "one; the full text is in the run's reports.txt either way, and 0 "
            "prints every report inline (default: %(default)s)."
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

    # Under `-n`, only the controller records. It is the one process that sees
    # the whole run: xdist ships every worker's TestReport back to it, and a
    # serialized report carries the traceback, the captured stdout/stderr/log
    # sections and the durations -- everything add_report() reads. A worker
    # that recorded too would claim a second runs-NNNN and write a partial
    # index.jsonl into it, which is the scattering that made agent mode and
    # xdist incompatible in the first place.
    #
    # What the controller cannot see is the part that never goes through a
    # report: note() and attach() run inside the worker. With no runtime
    # registered there they fall back to printing, and that print lands in the
    # worker's captured output -- which *is* shipped, so a note still reaches
    # the test's .log, just not notes.jsonl or the index. See the xdist
    # section of the README.
    if _xdist_role(config) == "worker":
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

    agent_dir = cast("str", config.getoption("agent_dir"))
    top_root = Path(agent_dir)
    if not top_root.is_absolute():
        top_root = config.rootpath / top_root
    try:
        run_number, root = next_run_dir(top_root)
    except OSError as error:
        # A read-only checkout, a CI workspace mounted read-only, a
        # .pytest-agent left behind root-owned by a container, a full disk.
        # Agent mode is a way of watching a test run, so it must never be the
        # reason one cannot happen: it takes itself out of the picture and
        # leaves pytest exactly as it would have been. Loudly, on stderr,
        # because a run that quietly stopped recording is a trap -- the next
        # `pytest-agent last-failures` would answer from a stale run.
        sys.stderr.write(
            f"pytest-agent: agent mode is OFF for this run -- cannot write to "
            f"{top_root}: {error}\n"
            "Tests run normally, with pytest's own output. Point --agent-dir "
            "(or PYTEST_AGENT_DIR) somewhere writable to turn it back on.\n",
        )
        return

    # After the run directory exists, because that is where the reporter's
    # output now goes.
    terminal_log_path = root / TERMINAL_LOG_NAME
    terminal_log = _redirect_terminal_reporter(config, terminal_log_path)

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
    # A stuck dump is faulthandler dumping *this* process's threads. On an
    # xdist controller the hung test is in another process entirely, so the
    # dump would show the controller idling in its own event loop and name an
    # innocent test -- worse than no dump, because it reads like evidence.
    # Turned off rather than reimplemented: doing it properly means dumping
    # from inside the worker, which is worker-side recording (see the README).
    distributed = _xdist_role(config) == "controller"
    if distributed:
        stuck_after = 0.0
    status_interval = cast("float", config.getoption("agent_status_interval"))
    max_summary_lines = cast("int", config.getoption("agent_max_summary_lines"))
    runtime = AgentRuntime(
        config,
        root=root,
        top_root=top_root,
        run_number=run_number,
        keep_runs=keep_runs,
        heartbeat_interval=heartbeat_interval,
        stuck_after=stuck_after,
        status_interval=status_interval,
        max_summary_lines=max_summary_lines,
        terminal=_REAL_TERMINAL,
        terminal_log=terminal_log,
        terminal_log_path=terminal_log_path,
        label=label,
        autodetected_via=autodetected_via,
        distributed=distributed,
    )
    config.pluginmanager.register(runtime, RUNTIME_PLUGIN_NAME)
    # So pytest_agent.note() can find this session without a fixture to carry
    # it -- the whole point of that entry point is being callable from inside
    # the code under test.
    push_runtime(runtime)


def _redirect_terminal_reporter(config: pytest.Config, path: Path) -> TextIO | None:
    """Point the builtin terminal reporter at *path* instead of the terminal.

    Fully unregistering it (config.pluginmanager.unregister(...)) was tried
    first and broke pytest's own assertion-rewrite comparison output:
    Config.get_terminal_writer() asserts the "terminalreporter" plugin is
    still registered, and pytest_assertrepr_compare calls that internally to
    get a highlighter for every failing comparison. Instead the reporter stays
    registered (so that internal lookup keeps succeeding) and only its output
    file is swapped.

    A file rather than os.devnull, which is what this was for a long time.
    Sending it to devnull destroys more than pytest's own per-test reporting:
    every plugin that reports through the terminal writer -- pytest-cov's
    coverage table, `--durations`, `--junit-xml`'s "generated xml file" line --
    was silently producing nothing in agent mode. Asking for a report and
    getting no report and no error is the worst way to lose output, and it
    made agent mode a bad citizen of the plugin API. Now nothing is destroyed:
    it is one file read away, and the sections with no agent-mode equivalent
    are surfaced by AgentRuntime.pytest_terminal_summary.

    Not a tty, so TerminalWriter emits no colour codes and the file stays
    plain text.
    """
    terminal_reporter = config.pluginmanager.get_plugin("terminalreporter")
    if terminal_reporter is None:
        return None
    log = path.open("w", encoding="utf-8")
    terminal_reporter._tw = create_terminal_writer(config, log)  # type: ignore[reportPrivateUsage] -- see docstring above  # noqa: SLF001
    return log


def pytest_unconfigure(config: pytest.Config) -> None:
    runtime = config.pluginmanager.get_plugin(RUNTIME_PLUGIN_NAME)
    if runtime is not None:
        agent_runtime = cast("AgentRuntime", runtime)
        pop_runtime(agent_runtime)
        config.pluginmanager.unregister(runtime)
        # Last: the terminal reporter writes its stats line after the
        # terminal-summary hook, so this file is live until pytest is done
        # with the session entirely.
        agent_runtime.close_terminal_log()
