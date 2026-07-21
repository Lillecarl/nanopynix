from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING

from pytest_agent._capture import TestRecorder

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from pytest_agent._terminal import RealTerminal

_POLL_INTERVAL_S = 2.0


class AgentRuntime:
    """The registered plugin object for one agent-mode pytest session.

    Owns the per-test file capture (via TestRecorder) and a background
    thread that prints short progress/stuck lines -- the only CLI output
    agent mode produces while tests are running.
    """

    def __init__(
        self,
        config: pytest.Config,
        *,
        root: Path,
        stuck_after: float,
        heartbeat_interval: float,
        terminal: RealTerminal | None,
    ) -> None:
        self.config = config
        self.root = root
        self.stuck_after = stuck_after
        self.heartbeat_interval = heartbeat_interval
        self.terminal = terminal
        self.recorder = TestRecorder(root)

        self.counts: dict[str, int] = dict.fromkeys(
            ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "collect_error"), 0
        )

        self.current_nodeid: str | None = None
        self.session_started_at = 0.0
        self.last_progress_at = 0.0
        self.last_heartbeat_at = 0.0
        self.last_stuck_notice_at = 0.0

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _print(self, line: str) -> None:
        if self.terminal is not None:
            self.terminal.write_line(f"[pytest-agent] {line}")

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        self.recorder.start()
        now = time.monotonic()
        self.session_started_at = now
        self.last_progress_at = now
        self.last_heartbeat_at = now
        self._print(f"writing full per-test detail to: {self.root.resolve()}")
        self._thread = threading.Thread(target=self._watch, name="pytest-agent-watcher", daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        while not self._stop_event.wait(_POLL_INTERVAL_S):
            now = time.monotonic()
            idle = now - self.last_progress_at
            if idle >= self.stuck_after and now - self.last_stuck_notice_at >= self.stuck_after:
                self.last_stuck_notice_at = now
                current = self.current_nodeid or "<collection>"
                self._print(f"still running {current} -- no test has finished in {idle:.0f}s, may be stuck")
            elif idle < self.stuck_after and now - self.last_heartbeat_at >= self.heartbeat_interval:
                self.last_heartbeat_at = now
                self._print(self._progress_line())

    def _progress_line(self) -> str:
        elapsed = time.monotonic() - self.session_started_at
        finished = sum(self.counts.values())
        return (
            f"{elapsed:.0f}s elapsed | {finished} finished "
            f"({self.counts['passed']} passed, {self.counts['failed']} failed, "
            f"{self.counts['error']} error, {self.counts['skipped']} skipped) "
            f"| running: {self.current_nodeid or '?'}"
        )

    def pytest_runtest_logstart(self, nodeid: str, location: object) -> None:
        del location
        self.current_nodeid = nodeid
        self.last_progress_at = time.monotonic()

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        category, _letter, _word = self.config.hook.pytest_report_teststatus(report=report, config=self.config)
        self.last_progress_at = time.monotonic()
        record = self.recorder.add_report(report, category or "")
        if record is None:
            return
        self.counts[record["outcome"]] = self.counts.get(record["outcome"], 0) + 1
        self.current_nodeid = None

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.outcome != "failed":
            return
        self.last_progress_at = time.monotonic()
        self.counts["collect_error"] += 1
        self.recorder.add_collect_error(report)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

        duration = time.monotonic() - self.session_started_at
        summary = {
            "exit_status": int(exitstatus),
            "duration_s": round(duration, 3),
            "counts": dict(self.counts),
        }
        (self.root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

        self._print(f"done in {duration:.1f}s -- {self._final_counts_line()}")
        failed_nodeids = self.recorder.nodeids_with_outcome({"failed", "error", "collect_error"})
        if failed_nodeids:
            self._print(f"{len(failed_nodeids)} failed/errored:")
            for nodeid in failed_nodeids:
                self._print(f"  {nodeid}")
        self._print(f"full detail: {self.root.resolve()} (see index.jsonl)")

    def _final_counts_line(self) -> str:
        return (
            f"{self.counts['passed']} passed, {self.counts['failed']} failed, "
            f"{self.counts['error']} error, {self.counts['skipped']} skipped, "
            f"{self.counts['collect_error']} collection errors"
        )
