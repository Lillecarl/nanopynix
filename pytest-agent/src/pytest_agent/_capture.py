from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

_UNSAFE_PATH_CHARS = re.compile(r"[\\/]")

# Precedence for picking one overall outcome out of a test's setup/call/teardown
# phase categories (as classified by pytest's own pytest_report_teststatus
# hook, not reimplemented here) -- "worst" wins.
_CATEGORY_PRECEDENCE = ("error", "failed", "xpassed", "skipped", "xfailed", "passed")


def _sanitize(segment: str) -> str:
    return _UNSAFE_PATH_CHARS.sub("_", segment)


def nodeid_to_relpath(nodeid: str) -> Path:
    """Map a test nodeid to a filesystem-safe relative path, mirroring the
    test file's own path as directories, e.g.
    'tests/test_foo.py::test_bar[a/b]' -> 'tests/test_foo.py/test_bar[a_b]'.
    """
    file_part, _, test_part = nodeid.partition("::")
    test_part = _sanitize(test_part.replace("::", "__")) or "_module_"
    return Path(file_part) / test_part


class TestRecorder:
    """Writes one log + JSON file per test, plus a running index.jsonl, under
    *root*. This is the whole point of agent mode: nothing an agent could
    need is only ever printed to the terminal.
    """

    __test__ = False  # not a pytest test class, despite the name

    def __init__(self, root: Path) -> None:
        self.root = root
        self.collect_errors_dir = root / "collect_errors"
        self.index_path = root / "index.jsonl"
        self._pending: dict[str, list[tuple[pytest.TestReport, str]]] = {}
        self._records: list[dict[str, Any]] = []

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text("", encoding="utf-8")

    def add_report(self, report: pytest.TestReport, category: str) -> dict[str, Any] | None:
        """Feed one setup/call/teardown phase report. Returns the finalized
        record once the teardown phase (always the last phase pytest emits
        for a given test, whether or not setup/call succeeded) arrives.
        """
        entries = self._pending.setdefault(report.nodeid, [])
        entries.append((report, category))
        if report.when != "teardown":
            return None
        del self._pending[report.nodeid]
        return self._finalize(report.nodeid, entries)

    def _finalize(self, nodeid: str, entries: list[tuple[pytest.TestReport, str]]) -> dict[str, Any]:
        categories = {category for _report, category in entries if category}
        outcome = next((c for c in _CATEGORY_PRECEDENCE if c in categories), "passed")
        duration = sum(report.duration for report, _category in entries)

        sections = [f"nodeid: {nodeid}", f"outcome: {outcome}", f"duration_s: {duration:.3f}"]
        has_traceback = False
        for report, _category in entries:
            if report.longreprtext:
                has_traceback = True
                sections.append(f"=== TRACEBACK ({report.when}) ===\n{report.longreprtext}")
            if report.capstdout:
                sections.append(f"=== STDOUT ({report.when}) ===\n{report.capstdout}")
            if report.capstderr:
                sections.append(f"=== STDERR ({report.when}) ===\n{report.capstderr}")
            caplog = getattr(report, "caplog", "")
            if caplog:
                sections.append(f"=== LOG ({report.when}) ===\n{caplog}")

        rel = nodeid_to_relpath(nodeid)
        out_dir = self.root / rel.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / f"{rel.name}.log"
        json_path = out_dir / f"{rel.name}.json"
        log_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")

        record = {
            "nodeid": nodeid,
            "outcome": outcome,
            "duration_s": round(duration, 3),
            "log_file": str(log_path.relative_to(self.root)),
            "has_traceback": has_traceback,
        }
        json_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        self._append_index(record)
        return record

    def add_collect_error(self, report: pytest.CollectReport) -> dict[str, Any]:
        nodeid = report.nodeid or "unknown"
        self.collect_errors_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.collect_errors_dir / f"{_sanitize(nodeid) or 'unknown'}.log"
        log_path.write_text(
            f"nodeid: {nodeid}\noutcome: collect_error\n\n{report.longreprtext}\n",
            encoding="utf-8",
        )
        record = {
            "nodeid": nodeid,
            "outcome": "collect_error",
            "duration_s": 0.0,
            "log_file": str(log_path.relative_to(self.root)),
            "has_traceback": True,
        }
        self._append_index(record)
        return record

    def _append_index(self, record: dict[str, Any]) -> None:
        self._records.append(record)
        with self.index_path.open("a", encoding="utf-8") as index_file:
            index_file.write(json.dumps(record) + "\n")

    def nodeids_with_outcome(self, outcomes: set[str]) -> list[str]:
        return [record["nodeid"] for record in self._records if record["outcome"] in outcomes]
