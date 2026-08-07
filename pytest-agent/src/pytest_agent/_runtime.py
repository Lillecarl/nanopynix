from __future__ import annotations

import contextlib
import faulthandler
import json
import os
import platform
import re
import signal
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from pytest_agent._capture import (
    TestRecorder,
    abbreviate_nodeid,
    nodeid_is_evident_from,
    stuck_dump_path,
)
from pytest_agent._history import (
    append_run_record,
    git_revision,
    prune_old_runs,
    release_run_lock,
    write_run_meta,
)
from pytest_agent._paths import display_path

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path
    from types import FrameType
    from typing import TextIO

    from pytest_agent._terminal import RealTerminal

RUNTIME_PLUGIN_NAME = "pytest-agent-runtime"

# Stack dumps written for one long-running test before giving up on it. Five
# is enough to tell a wedged test (identical stacks) from a slow one (the
# stack moves), without an unbounded file if a run is left going overnight.
MAX_STUCK_DUMPS = 5

# Terminal lines the notes block may take before the rest is left to
# notes.jsonl. Notes are deliberate output -- somebody asked for them, so the
# budget is generous -- but a probe inside a loop over 300 tests must not bury
# the failure list above it.
MAX_NOTE_LINES = 60

# Tests named individually when their detail could not be written. A run
# where every test hits this (a read-only agent dir) must not print one line
# per test on top of everything else it is already failing to do.
MAX_CAPTURE_ERRORS_SHOWN = 10

# Where the builtin terminal reporter's output goes in agent mode.
TERMINAL_LOG_NAME = "terminal.txt"

# Where a run says what it is doing *right now*, for a reader outside the
# process. The third of three files, and the division between them is by
# lifetime: meta.json is what the run is (written once, at the start),
# summary.json is what it did (written once, at the end), and this one is
# where it has got to (rewritten while it goes, gone stale the moment the
# process dies).
#
# It exists because the running nodeid and its age were in memory only. Every
# other question about a live run could be answered from disk -- which tests
# finished, whether the directory is still claimed -- but not "what is it
# waiting on", which is the only question a hung run raises.
STATUS_FILE_NAME = "status.json"

# How often that file is rewritten. `pytest-agent watch` polls at two seconds,
# so a finer interval buys a reader nothing and costs the run one more write.
DEFAULT_STATUS_INTERVAL = 2.0

# The outcomes counted for a run, as a fixed tuple rather than only as the
# keys of the dict built from it. The watcher thread snapshots the counts
# while the main thread is adding to them, and reading through a known key
# list cannot raise "dictionary changed size during iteration" the way
# copying the dict wholesale could.
COUNT_NAMES = ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "collect_error")

# ...and where the end-of-run reports other plugins wrote through it are saved
# a second time, on their own. A coverage table is an artifact somebody wants
# to read whole; terminal.txt is a transcript it happens to appear in.
REPORTS_LOG_NAME = "reports.txt"

# Terminal lines an end-of-run report from another plugin may take inline,
# unless --agent-max-summary-lines says otherwise. Past this it is replaced by
# a pointer to reports.txt rather than shown in part.
#
# Showing part of it was the previous behaviour and it is the thing being
# fixed, not the bound. A `pytest --cov --cov-report=term-missing` run of the
# suite this package was written for produces 88 lines; head-20/tail-15 left
# the reader with column headings, TOTAL, and none of the rows in between --
# which is what a coverage table *is*. That is the right shape for a traceback,
# whose ends carry it, and the wrong one for a table, whose value is spread
# evenly across its middle. A fragment of a table is worse than a pointer to
# the whole one: it reads as the whole one. (An agent did read it that way, and
# reported most of this codebase as uncovered.)
#
# So the choice past the bound is binary, and the number only decides which
# reports are short enough to be worth reading in passing. 40 is about half a
# screen: `--durations=25` and `--junit-xml`'s one-liner fit, a coverage table
# of any real project does not.
DEFAULT_MAX_TERMINAL_SUMMARY_LINES = 40

# The `TOTAL   1234   56   96%` row coverage's own terminal reporter writes,
# and the `Total coverage: 96.85%` line pytest-cov adds under --cov-fail-under.
# Read out of the text rather than off pytest-cov's plugin object on purpose:
# nothing else here knows a plugin by name, `cov_controller` is private and has
# moved between releases, and pytest-agent does not depend on pytest-cov at all
# -- so a format change costs the extra line, while an attribute rename would
# have cost an AttributeError at the end of somebody's run.
_COVERAGE_TOTAL_PATTERNS = (
    # First percentage after TOTAL, not the last: Cover is the only percent
    # column, and anchoring at end-of-line would miss a table whose TOTAL row
    # carries a Missing column too.
    re.compile(r"^TOTAL\b.*?(\d+(?:\.\d+)?%)"),
    re.compile(r"[Tt]otal coverage:\s*(\d+(?:\.\d+)?%)"),
)


def coverage_total(lines: list[str]) -> str | None:
    """The overall percentage a coverage report ends on, if one is in ``lines``."""
    for pattern in _COVERAGE_TOTAL_PATTERNS:
        for line in reversed(lines):
            found = pattern.search(line)
            if found is not None:
                return found.group(1)
    return None


class AgentRuntime:
    """The registered plugin object for one agent-mode pytest session.

    Owns the per-test file capture (via TestRecorder) and a background
    thread that prints one progress line on a fixed interval -- the only CLI
    output agent mode produces while tests are running. A human or agent
    watching just that line can tell whether things are moving (counts and
    the running nodeid change between prints) or stuck (elapsed keeps
    climbing while nothing else does), without a separate stuck-detection
    mode to configure.
    """

    def __init__(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
        self,
        config: pytest.Config,
        *,
        root: Path,
        top_root: Path,
        run_number: int,
        keep_runs: int,
        heartbeat_interval: float,
        stuck_after: float,
        status_interval: float = DEFAULT_STATUS_INTERVAL,
        max_summary_lines: int = DEFAULT_MAX_TERMINAL_SUMMARY_LINES,
        terminal: RealTerminal | None,
        terminal_log: TextIO | None = None,
        terminal_log_path: Path | None = None,
        label: str | None = None,
        autodetected_via: str | None = None,
        distributed: bool = False,
    ) -> None:
        self.config = config
        self.root = root
        self.top_root = top_root
        self.run_number = run_number
        self.label = label
        self.keep_runs = keep_runs
        self.heartbeat_interval = heartbeat_interval
        self.stuck_after = stuck_after
        self.status_interval = status_interval
        self.max_summary_lines = max_summary_lines
        self.terminal = terminal
        self.terminal_log = terminal_log
        self.terminal_log_path = terminal_log_path
        self.autodetected_via = autodetected_via
        self.distributed = distributed
        self.recorder = TestRecorder(root, rootpath=config.rootpath)
        self.started_at_iso = ""

        self.counts: dict[str, int] = dict.fromkeys(COUNT_NAMES, 0)
        self.total_collected = 0

        self.session_started_at = 0.0
        self.killed_by: str | None = None

        # The running test and when it started, as one attribute rather than
        # two: the watchdog thread reads it while the main thread replaces it,
        # and a single assignment can't be caught halfway between tests.
        self._current: tuple[str, float] | None = None
        self._stuck_dumps: dict[str, int] = {}
        self._prev_sigterm: Callable[[int, FrameType | None], object] | int | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def current_nodeid(self) -> str | None:
        current = self._current
        return current[0] if current is not None else None

    def _print(self, line: str) -> None:
        if self.terminal is not None:
            self.terminal.write_line(f"[pytest-agent] {line}")

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        self.recorder.start()
        self.session_started_at = time.monotonic()
        self._install_sigterm_handler()
        self.started_at_iso = datetime.now(UTC).isoformat()
        self._write_meta()
        if self.autodetected_via is not None:
            self._print(
                f"auto-activated: found {self.autodetected_via} in the environment "
                "(set PYTEST_AGENT_NO_AUTODETECT=1 to disable this)",
            )
        # Before the thread starts, so the file is there for a reader that
        # attached to this run the moment its directory appeared. Waiting for
        # the first tick would leave a window in which the run looks dead.
        self._write_status()
        named = f"run {self.run_number}" if self.label is None else f"run {self.run_number} [{self.label}]"
        # The pid is on this line because the thing people need it for is
        # killing a run that will not stop, and the alternative is
        # `pkill -f <some pattern>`. That pattern matches every process whose
        # command line contains it, including the shell script that wrote the
        # pattern -- which kills the wrong process, and looks like the run
        # ending by itself. meta.json has carried the pid all along; nobody
        # opens a file to find out how to stop something.
        self._print(f"{named}, pid {os.getpid()}: writing full per-test detail to: {self.root.resolve()}")
        if self.distributed:
            self._print(
                "xdist: recording from the controller -- tracebacks, captured output and "
                "durations are complete; note()/attach() appear inline in each test's .log "
                "instead of notes.jsonl, and stuck-test dumps are off",
            )
        self._thread = threading.Thread(target=self._watch, name="pytest-agent-watcher", daemon=True)
        self._thread.start()

    def _write_meta(self) -> None:
        """Describe this run on disk before running a single test.

        Everything here is knowable at session start, and all of it is only
        useful before the run ends: the label so `--run <label>` can find a
        suite that is still going, the pid and args so a run found half-written
        in the archive can be tied back to the process and command that made
        it. What the run *did* goes in summary.json at the end.
        """
        # Tolerated rather than fatal: without meta.json this run cannot be
        # found by --run <label>, which is a lesser thing to lose than the run.
        with contextlib.suppress(OSError):
            write_run_meta(
                self.root,
                {
                    "run": self.run_number,
                    "run_dir": self.root.name,
                    "label": self.label,
                    "started_at": self.started_at_iso,
                    "pid": os.getpid(),
                    "args": list(self.config.invocation_params.args),
                },
            )

    def _write_status(self) -> None:
        """Say where this run has got to, for a reader in another process.

        Whole or not at all, like meta.json and summary.json: this file is
        rewritten every couple of seconds and read by a process that has no
        way to know it caught one mid-write, so a torn read would be a wrong
        answer rather than a missing one.

        The age of the running test travels as an age and not as a start time.
        `time.monotonic()` has no fixed origin, so its values mean nothing in
        another process; a reader adds its own `now - written_at` to this.

        Failure is tolerated for the reason the whole of agent mode tolerates
        it: this is a way of watching a test run, and it must never be the
        reason one stops. A run whose disk filled keeps running, and the
        reader sees a stale file and reports the run as dead -- which is the
        honest answer from out there.
        """
        current = self._current
        running_since = None if current is None else round(time.monotonic() - current[1], 3)
        status = {
            "run": self.run_number,
            "written_at": time.time(),
            "elapsed_s": round(time.monotonic() - self.session_started_at, 3),
            "running": None if current is None else current[0],
            "running_since_s": running_since,
            # Through COUNT_NAMES rather than dict(self.counts): the main
            # thread is writing to that dict while this thread reads it.
            "counts": {name: self.counts.get(name, 0) for name in COUNT_NAMES},
            "total_collected": self.total_collected,
            # So a reader does not report a stuck test that is not one. Under
            # xdist several tests run at once and `running` names an arbitrary
            # one of them, which makes its age meaningless.
            "distributed": self.distributed,
        }
        with contextlib.suppress(OSError):
            temp_path = self.root / f"{STATUS_FILE_NAME}.tmp"
            temp_path.write_text(json.dumps(status) + "\n", encoding="utf-8")
            temp_path.replace(self.root / STATUS_FILE_NAME)

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: object, ids: list[str]) -> None:
        """The collected total, which an xdist controller has no other way to know.

        Under `-n` the controller does not collect: the workers do, and
        ``session.items`` stays empty in this process, so the progress line
        would read ``tot=?`` for the whole run. Every worker collects the same
        set (xdist refuses to start if they disagree), so the first one to
        report settles it. ``optionalhook`` because this hookspec only exists
        when xdist is installed.
        """
        del node
        self.total_collected = max(self.total_collected, len(ids))

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.total_collected = len(session.items)

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_terminal_summary(self) -> Generator[None]:
        """Print what other plugins reported at the end of the run.

        Agent mode replaces pytest's own per-test reporting, but it has no
        equivalent for an end-of-run report from another plugin: a coverage
        table, `--durations`, the path `--junit-xml` just wrote. Those go
        through the terminal writer, which agent mode has redirected to a
        file, so before this they were asked for and silently never appeared.

        `trylast` makes this the *innermost* wrapper, which is what selects
        the right content. Plain (non-wrapper) hookimpls -- pytest-cov's,
        `--durations` in _pytest.runner, `--junit-xml`'s -- all run inside
        every wrapper, so they land in this window. TerminalReporter's own
        wrapper writes the failure tracebacks before the yield and the short
        summary after it, both outside this window, and both things agent
        mode already reports in its own form.
        """
        log = self.terminal_log
        path = self.terminal_log_path
        if log is None or path is None:
            return (yield)
        # Byte offsets from stat rather than TextIO.tell(), whose return value
        # is an opaque cookie that must not be used as a length.
        log.flush()
        start = path.stat().st_size
        try:
            return (yield)
        finally:
            log.flush()
            self._print_terminal_summary(path.read_bytes()[start:].decode("utf-8", errors="replace"))

    def _print_terminal_summary(self, text: str) -> None:
        lines = [line.rstrip() for line in text.splitlines()]
        # Leading and trailing blank lines are separators from a report that
        # assumed it was sitting in the middle of pytest's own output.
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return

        log_path = self._write_reports_file(lines)
        self._print(f"also reported by other plugins ({log_path}):")
        self._write_report_lines(lines, log_path)
        total = coverage_total(lines)
        if total is not None:
            # Last line of the run, prefixed and on its own. The percentage is
            # the reason `--cov` was passed, so it is the one part of the
            # report that should not need the file -- and with the table held
            # back above, it otherwise would.
            self._print(f"coverage: {total}")

    def _write_report_lines(self, lines: list[str], log_path: str) -> None:
        if self.terminal is None:
            return
        budget = self.max_summary_lines
        # The first line is kept even when the rest is not: a report's own
        # banner ("==== tests coverage ====") is what says which report is
        # waiting in the file, and a bare count would not.
        shown = lines if budget <= 0 or len(lines) <= budget else lines[:1]
        for line in shown:
            # Written raw, without the [pytest-agent] prefix the rest of this
            # output carries: these are somebody else's aligned tables, and a
            # prefix on every row makes a coverage report unreadable.
            self.terminal.write_line(line)
        if len(shown) == len(lines):
            return
        # The path is repeated here, resolved, rather than left to the pointer
        # line above: this marker is what somebody stops on when the content
        # they came for is missing, and a filename mentioned once further up
        # reads as a footnote about some other file.
        self.terminal.write_line(
            f"... {len(lines) - len(shown)} more report lines not shown; full text in {log_path} "
            f"(--agent-max-summary-lines=0 prints them here) ...",
        )

    def _write_reports_file(self, lines: list[str]) -> str:
        """Save the other-plugins block on its own, and say where it went.

        ``terminal.txt`` has all of this already, but it is the raw pytest
        stream -- session header, tracebacks, short summary -- with the report
        somewhere inside it. A pointer into a file you then have to search
        through is a pointer that gets skipped: in the case this was written
        for, it was printed with the right path and not followed, and the
        truncated terminal copy was believed instead. This file is *only* the
        report, so following the pointer needs no judgement about which part
        of it was meant.
        """
        path = self.root / REPORTS_LOG_NAME
        try:
            path.write_text("\n".join([*lines, ""]), encoding="utf-8")
        except OSError:
            # Same reasoning as everywhere else agent mode writes: it must
            # never be the reason a run is worse off. terminal.txt is written
            # by the redirect, on a handle opened at session start, so it can
            # still be there when this write fails.
            return display_path(self.root / TERMINAL_LOG_NAME)
        return display_path(path)

    def close_terminal_log(self) -> None:
        """Close the redirected terminal-reporter output.

        Called from pytest_unconfigure rather than sessionfinish: the reporter
        keeps writing after the terminal-summary hook (its stats line), and a
        closed file there would turn a finished run into a ValueError.
        """
        log = self.terminal_log
        self.terminal_log = None
        if log is None:
            return
        with contextlib.suppress(OSError, ValueError):
            # Already closed, or the file went away under us. Nothing here is
            # worth failing a finished run over.
            log.close()

    def _install_sigterm_handler(self) -> None:
        """Turn SIGTERM into the interrupt pytest already knows how to handle.

        `timeout 500 pytest tests` -- the invocation this repo documents -- ends
        a hung run with SIGTERM, and its default action kills the process
        outright: no summary, no history entry, no record of which test was
        running, so the one run that most needed explaining is the one that
        leaves nothing behind. Raising KeyboardInterrupt instead puts the run on
        pytest's existing graceful-interrupt path, which calls sessionfinish --
        every line below it then works unchanged, and a killed run degrades
        instead of vanishing.
        """
        if threading.current_thread() is not threading.main_thread():
            return
        previous = signal.getsignal(signal.SIGTERM)
        if previous is not signal.SIG_DFL:
            # Someone already owns SIGTERM: a handler of their own, or SIG_IGN
            # inherited from a parent that meant it. Either way their semantics
            # are deliberate and outrank ours.
            return
        try:
            signal.signal(signal.SIGTERM, self._on_sigterm)
        except (ValueError, OSError):
            # Not the main thread of the main interpreter, or a platform
            # without settable SIGTERM. Nothing to recover -- the run simply
            # keeps the default behaviour it had before this call.
            return
        self._prev_sigterm = previous

    def _on_sigterm(self, signum: int, frame: FrameType | None) -> None:
        del frame
        # Restore before raising, not after: a second SIGTERM -- from
        # `timeout --kill-after`, or a caller who waited long enough -- must
        # take the default action and kill us, rather than re-entering here
        # while sessionfinish is halfway through writing summary.json.
        self._restore_sigterm()
        self.killed_by = signal.Signals(signum).name
        raise KeyboardInterrupt(f"pytest-agent: {self.killed_by} received")

    def _restore_sigterm(self) -> None:
        previous, self._prev_sigterm = self._prev_sigterm, None
        if previous is None:
            return
        with contextlib.suppress(ValueError, OSError):
            # Same conditions as installing it; failing to restore a handler
            # during shutdown changes nothing that matters.
            signal.signal(signal.SIGTERM, previous)

    def _watch(self) -> None:
        """Tick until the session ends, printing progress and checking for hangs.

        The two jobs have separate intervals, so the loop runs at the finer of
        them and prints on the coarser. Ticking at the heartbeat alone
        quantized --agent-stuck-after to it: a stuck-after below the heartbeat
        dumped late, and a test that wedged and was killed between two ticks
        left no stack at all -- the one case the dumps exist for.
        """
        tick = self._tick_interval()
        if tick is None:
            return
        since_heartbeat = 0.0
        since_status = 0.0
        while not self._stop_event.wait(tick):
            since_heartbeat += tick
            since_status += tick
            if self.heartbeat_interval > 0 and since_heartbeat >= self.heartbeat_interval:
                since_heartbeat = 0.0
                self._print(self._progress_line())
            if self.status_interval > 0 and since_status >= self.status_interval:
                since_status = 0.0
                self._write_status()
            self._check_stuck()

    def _tick_interval(self) -> float | None:
        """How often the watcher wakes, or None when it has nothing to do.

        A non-positive interval means "off" for all three options, matching
        what --agent-stuck-after already documented. Before this,
        --agent-heartbeat 0 was an unguarded Event.wait(0): a spin loop that
        pegged a core and printed hundreds of thousands of progress lines
        through a short run.

        --agent-status-interval joins the other two here rather than being a
        constant, so that turning every interval off still stops the thread
        entirely. A constant would keep a thread alive, writing a file every
        two seconds, in a run that asked for no thread at all.
        """
        intervals = [value for value in (self.heartbeat_interval, self.stuck_after, self.status_interval) if value > 0]
        return min(intervals) if intervals else None

    def _check_stuck(self) -> None:
        """Dump the stack of a test that has been running an implausibly long time.

        The half of kill-survival that works when the signal handler can't: a
        thread wedged inside a C call never reaches the interpreter loop, so it
        never runs a Python handler and never gets to write anything on the way
        out. Dumping while it is still hung sidesteps that entirely -- by the
        time anyone kills the run, the stack that explains it is already on disk.
        """
        current = self._current
        if current is None or self.stuck_after <= 0:
            return
        nodeid, started_at = current
        elapsed = time.monotonic() - started_at
        dumped = self._stuck_dumps.get(nodeid, 0)
        if dumped >= MAX_STUCK_DUMPS or elapsed < self.stuck_after * (dumped + 1):
            return
        self._stuck_dumps[nodeid] = dumped + 1
        path = self.stuck_path_for(nodeid)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stack_file:
                stack_file.write(f"\n=== still running after {elapsed:.0f}s: {nodeid} ===\n")
                stack_file.flush()
                # all_threads: a test that stops making progress is usually
                # waiting on another thread, and the main thread's stack alone
                # shows the wait without showing what it is waiting for.
                faulthandler.dump_traceback(file=stack_file, all_threads=True)
        except OSError as error:
            self._print(f"could not write stack dump for {abbreviate_nodeid(nodeid)}: {error}")
            return
        # "still running", not "stuck": a slow test and a hung one look
        # identical from here, and only the stacks can tell them apart.
        self._print(
            f"still running after {elapsed:.0f}s: {abbreviate_nodeid(nodeid)} -- stack dumped to {display_path(path)}",
        )

    def stuck_path_for(self, nodeid: str) -> Path:
        """Where stack dumps for *nodeid* go. See `_capture.stuck_dump_path`."""
        return stuck_dump_path(self.root, nodeid)

    def _progress_line(self) -> str:
        elapsed = time.monotonic() - self.session_started_at
        finished = sum(self.counts.values())
        return (
            f"{elapsed:.0f}s pass={self.counts['passed']} fail={self.counts['failed']} "
            f"done={finished} tot={self.total_collected or '?'} "
            f"cur={abbreviate_nodeid(self.current_nodeid) if self.current_nodeid else '?'}"
        )

    def pytest_runtest_logstart(self, nodeid: str, location: object) -> None:
        del location
        self._current = (nodeid, time.monotonic())

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        category, _letter, _word = self.config.hook.pytest_report_teststatus(report=report, config=self.config)
        record = self.recorder.add_report(report, category or "")
        if record is None:
            return
        self.counts[record["outcome"]] = self.counts.get(record["outcome"], 0) + 1
        self._current = None

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.outcome != "failed":
            return
        self.counts["collect_error"] += 1
        self.recorder.add_collect_error(report)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        # After the join, so nothing else is writing the file, and before
        # summary.json, so a reader never sees a status older than the run in
        # the window between the two. `running` is deliberately left as it
        # stands: a value here means the run was cut short mid-test, and that
        # nodeid is the most useful thing it can still say.
        self._write_status()
        self._restore_sigterm()

        duration = time.monotonic() - self.session_started_at
        # Whatever was still running when the session ended. Normally nothing:
        # logreport clears it at the end of every test. A value here means the
        # run was cut short mid-test, and that nodeid is the single most useful
        # thing the run produced.
        interrupted_at = self.current_nodeid
        record = {
            "run": self.run_number,
            "run_dir": self.root.name,
            "label": self.label,
            "hostname": platform.node(),
            "started_at": self.started_at_iso,
            "duration_s": round(duration, 3),
            "exit_status": int(exitstatus),
            "counts": dict(self.counts),
            "total_collected": self.total_collected,
            "args": list(self.config.invocation_params.args),
            "git_rev": git_revision(self.top_root),
            "killed_by": self.killed_by,
            "interrupted_at": interrupted_at,
        }
        summary_path = self.root / "summary.json"
        # Written whole or not at all: a second signal can land here, and a
        # half-written summary.json is worse than an absent one -- absent is
        # obvious, truncated reads as corrupt.
        temp_path = summary_path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(summary_path)
        append_run_record(self.top_root / "history.jsonl", record)
        prune_old_runs(self.top_root, self.keep_runs, protect=self.root)
        # Last, and after pruning: from here on this run is a finished archive
        # entry like any other, and a later run may prune it in its turn.
        release_run_lock(self.root)

        self._print(f"done in {duration:.1f}s -- {self._final_counts_line()}")
        self._print_interruption(interrupted_at)
        failed = self.recorder.records_with_outcome({"failed", "error", "collect_error"})
        if failed:
            self._print(f"{len(failed)} failed/errored:")
            for record in failed:
                # One line per failure: the resolved, shell-quoted log path
                # rather than the nodeid. Reconstructing that path by hand
                # means knowing the run number, mirroring the test file's
                # path, and quoting the brackets in a parametrized id --
                # three chances to get it wrong before reading a single line
                # of the failure -- while the nodeid is right there in the
                # path (and on the log's own first line). The nodeid is only
                # appended when the path can't be read back as one.
                line = display_path(self.root / record["log_file"])
                if not nodeid_is_evident_from(record["log_file"], record["nodeid"]):
                    # Deliberately the full nodeid, not an abbreviated one.
                    # This branch is reached precisely when the log path lost
                    # the id, so this copy is the only addressable form left --
                    # and `pytest-agent show` takes a nodeid or a unique
                    # substring, neither of which an elided middle is. The
                    # width cap is for lines that repeat on every tick; this
                    # list prints once.
                    line = f"{line}  ({record['nodeid']})"
                self._print(f"  {line}")
            if len(failed) > 1:
                # Only worth a line when there is actually a "do these share
                # one cause?" question to ask -- with a single failure the
                # log path above is already the whole answer.
                self._print("shared root cause? pytest-agent digest")
        self._print_capture_errors()
        self._print_notes()
        self._print(f"full detail: {self.root.resolve()} (see index.jsonl)")

    def _print_capture_errors(self) -> None:
        """Say when a test's outcome was recorded but its detail could not be.

        Rare, and silence would be the wrong kind of quiet: the run looks
        complete, and only a later `show` on that one test reveals there is
        nothing behind it. Named here, while the run is still on screen.
        """
        if self.recorder.index_error is not None:
            self._print(f"index.jsonl could not be written: {self.recorder.index_error}")
            self._print("  this run left no archive -- what is printed above is the whole record of it")
        affected = self.recorder.records_with_capture_error()
        if not affected:
            return
        self._print(f"{len(affected)} tests recorded an outcome but no detail file:")
        for record in affected[:MAX_CAPTURE_ERRORS_SHOWN]:
            # Full id, for the same reason as the failure list above: this is
            # a once-per-run list of tests to go and look at, and it is capped
            # at MAX_CAPTURE_ERRORS_SHOWN lines rather than by width.
            self._print(f"  {record['nodeid']}  ({record['capture_error']})")
        if len(affected) > MAX_CAPTURE_ERRORS_SHOWN:
            self._print(f"  +{len(affected) - MAX_CAPTURE_ERRORS_SHOWN} more (see capture_error in index.jsonl)")

    def _print_interruption(self, interrupted_at: str | None) -> None:
        """Say what was running when the run was cut short, and where its stack is.

        The question after a killed run is always "killed doing what?", and the
        answer is otherwise unrecoverable: the test never finished, so it has no
        log and no record, and the counts above say only that it isn't there.
        """
        if self.killed_by is not None and interrupted_at is None:
            self._print(f"interrupted by {self.killed_by} between tests")
        if interrupted_at is None:
            return
        reason = f"interrupted by {self.killed_by}" if self.killed_by is not None else "interrupted"
        self._print(f"{reason} while running: {interrupted_at}")
        # Only resolved when a dump was actually written, so the path is one
        # that already worked once. This runs while shutting down a run that
        # was killed -- an exception here would turn "degrades gracefully"
        # back into "vanishes, plus a traceback".
        if self._stuck_dumps.get(interrupted_at):
            self._print(f"  its stack, dumped while it ran: {display_path(self.stuck_path_for(interrupted_at))}")

    def _print_notes(self) -> None:
        """Echo whatever the run's tests deliberately recorded via `note()`.

        The one thing agent mode prints that isn't about pass/fail. A note is
        an explicit act -- somebody added `note(...)` to find something out --
        and answering that in the run itself is the whole point: a value only
        readable from a file costs a second turn to go and read it.
        """
        lines = self._note_lines()
        if not lines:
            return
        self._print("notes:")
        for line in lines[:MAX_NOTE_LINES]:
            self._print(line)
        if len(lines) > MAX_NOTE_LINES:
            dropped = len(lines) - MAX_NOTE_LINES
            self._print(f"  +{dropped} more lines: {display_path(self.recorder.notes_path)}")

    def _note_lines(self) -> list[str]:
        lines: list[str] = []
        for group in self.recorder.note_groups():
            entries = [note.line() for note in group.notes]
            entries += [f"attached: {display_path(self.root / name)}" for name in group.attachments]
            # A test with one short note reads better on one line with its
            # nodeid; anything more needs the nodeid as a heading.
            if len(entries) == 1 and "\n" not in entries[0]:
                lines.append(f"  {group.label}  {entries[0]}")
                continue
            lines.append(f"  {group.label}")
            for entry in entries:
                first, *rest = entry.split("\n")
                lines.append(f"    {first}")
                lines.extend(f"      {continuation}" for continuation in rest)
        return lines

    def _final_counts_line(self) -> str:
        return (
            f"{self.counts['passed']} passed, {self.counts['failed']} failed, "
            f"{self.counts['error']} error, {self.counts['skipped']} skipped, "
            f"{self.counts['collect_error']} collection errors"
        )
