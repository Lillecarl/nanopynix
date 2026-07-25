from __future__ import annotations

import contextlib
import faulthandler
import json
import platform
import signal
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pytest_agent._capture import TestRecorder, nodeid_is_evident_from, nodeid_to_relpath
from pytest_agent._history import append_run_record, git_revision, prune_old_runs, release_run_lock
from pytest_agent._paths import display_path

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import FrameType

    import pytest

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

    def __init__(  # noqa: PLR0913 tracked complexity/arg-count debt, see TODO.md
        self,
        config: pytest.Config,
        *,
        root: Path,
        top_root: Path,
        run_number: int,
        keep_runs: int,
        heartbeat_interval: float,
        stuck_after: float,
        terminal: RealTerminal | None,
        autodetected_via: str | None = None,
    ) -> None:
        self.config = config
        self.root = root
        self.top_root = top_root
        self.run_number = run_number
        self.keep_runs = keep_runs
        self.heartbeat_interval = heartbeat_interval
        self.stuck_after = stuck_after
        self.terminal = terminal
        self.autodetected_via = autodetected_via
        self.recorder = TestRecorder(root, rootpath=config.rootpath)
        self.started_at_iso = ""

        self.counts: dict[str, int] = dict.fromkeys(
            ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "collect_error"),
            0,
        )
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
        if self.autodetected_via is not None:
            self._print(
                f"auto-activated: found {self.autodetected_via} in the environment "
                "(set PYTEST_AGENT_NO_AUTODETECT=1 to disable this)",
            )
        self._print(f"run {self.run_number}: writing full per-test detail to: {self.root.resolve()}")
        self._thread = threading.Thread(target=self._watch, name="pytest-agent-watcher", daemon=True)
        self._thread.start()

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.total_collected = len(session.items)

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
        while not self._stop_event.wait(self.heartbeat_interval):
            self._print(self._progress_line())
            self._check_stuck()

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
            self._print(f"could not write stack dump for {nodeid}: {error}")
            return
        # "still running", not "stuck": a slow test and a hung one look
        # identical from here, and only the stacks can tell them apart.
        self._print(f"still running after {elapsed:.0f}s: {nodeid} -- stack dumped to {display_path(path)}")

    def stuck_path_for(self, nodeid: str) -> Path:
        """Where stack dumps for *nodeid* go: beside the test's log, not inside it.

        The log is written when a test finishes, and a test being dumped may
        never finish.
        """
        rel = nodeid_to_relpath(nodeid)
        return self.root / rel.parent / f"{rel.name}.stuck.txt"

    def _progress_line(self) -> str:
        elapsed = time.monotonic() - self.session_started_at
        finished = sum(self.counts.values())
        return (
            f"{elapsed:.0f}s pass={self.counts['passed']} fail={self.counts['failed']} "
            f"done={finished} tot={self.total_collected or '?'} cur={self.current_nodeid or '?'}"
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
                    line = f"{line}  ({record['nodeid']})"
                self._print(f"  {line}")
            if len(failed) > 1:
                # Only worth a line when there is actually a "do these share
                # one cause?" question to ask -- with a single failure the
                # log path above is already the whole answer.
                self._print("shared root cause? pytest-agent digest")
        self._print_notes()
        self._print(f"full detail: {self.root.resolve()} (see index.jsonl)")

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
