from __future__ import annotations

import json
import platform
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pytest_agent._capture import TestRecorder
from pytest_agent._history import append_run_record, git_revision, prune_old_runs

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from pytest_agent._terminal import RealTerminal

RUNTIME_PLUGIN_NAME = "pytest-agent-runtime"


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
        terminal: RealTerminal | None,
        autodetected_via: str | None = None,
    ) -> None:
        self.config = config
        self.root = root
        self.top_root = top_root
        self.run_number = run_number
        self.keep_runs = keep_runs
        self.heartbeat_interval = heartbeat_interval
        self.terminal = terminal
        self.autodetected_via = autodetected_via
        self.recorder = TestRecorder(root)
        self.started_at_iso = ""

        self.counts: dict[str, int] = dict.fromkeys(
            ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "collect_error"),
            0,
        )
        self.total_collected = 0

        self.current_nodeid: str | None = None
        self.session_started_at = 0.0

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _print(self, line: str) -> None:
        if self.terminal is not None:
            self.terminal.write_line(f"[pytest-agent] {line}")

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        self.recorder.start()
        self.session_started_at = time.monotonic()
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

    def _watch(self) -> None:
        while not self._stop_event.wait(self.heartbeat_interval):
            self._print(self._progress_line())

    def _progress_line(self) -> str:
        elapsed = time.monotonic() - self.session_started_at
        finished = sum(self.counts.values())
        return (
            f"{elapsed:.0f}s pass={self.counts['passed']} fail={self.counts['failed']} "
            f"done={finished} tot={self.total_collected or '?'} cur={self.current_nodeid or '?'}"
        )

    def pytest_runtest_logstart(self, nodeid: str, location: object) -> None:
        del location
        self.current_nodeid = nodeid

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        category, _letter, _word = self.config.hook.pytest_report_teststatus(report=report, config=self.config)
        record = self.recorder.add_report(report, category or "")
        if record is None:
            return
        self.counts[record["outcome"]] = self.counts.get(record["outcome"], 0) + 1
        self.current_nodeid = None

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.outcome != "failed":
            return
        self.counts["collect_error"] += 1
        self.recorder.add_collect_error(report)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

        duration = time.monotonic() - self.session_started_at
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
        }
        (self.root / "summary.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        append_run_record(self.top_root / "history.jsonl", record)
        prune_old_runs(self.top_root, self.keep_runs, protect=self.root)

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
