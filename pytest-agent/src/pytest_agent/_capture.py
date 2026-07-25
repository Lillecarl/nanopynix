from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pytest_agent._crash import crash_from_report, frames_from_report

if TYPE_CHECKING:
    import pytest

_UNSAFE_PATH_CHARS = re.compile(r"[\\/]")

# Precedence for picking one overall outcome out of a test's setup/call/teardown
# phase categories (as classified by pytest's own pytest_report_teststatus
# hook, not reimplemented here) -- "worst" wins.
_CATEGORY_PRECEDENCE = ("error", "failed", "xpassed", "skipped", "xfailed", "passed")

# The outcomes that have a crash worth extracting. A skip or xfail also
# carries a longrepr, but it describes a decision, not a failure, and
# grouping those in a digest would just be noise.
_CRASHING_OUTCOMES = frozenset({"failed", "error"})


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


def nodeid_is_evident_from(log_file: str, nodeid: str) -> bool:
    """Whether *log_file* already spells out *nodeid*, so printing both is redundant.

    True for the ordinary case, where the log path is the nodeid with ``::``
    written as a directory separator. False when the mapping lost something:
    a parametrized id containing ``/`` is sanitized to ``_``, and a collect
    error's log is named on an entirely different scheme -- in both cases the
    nodeid can't be read back off the path, so it has to be printed.
    """
    if not log_file.endswith(".log"):
        return False
    stem = log_file[: -len(".log")]
    directory, separator, name = stem.rpartition("/")
    return bool(separator) and f"{directory}::{name}" == nodeid


class TestRecorder:
    """Writes one log + JSON file per test, plus a running index.jsonl, under
    *root*. This is the whole point of agent mode: nothing an agent could
    need is only ever printed to the terminal.
    """

    __test__ = False  # not a pytest test class, despite the name

    def __init__(self, root: Path, *, rootpath: Path) -> None:
        self.root = root
        self.rootpath = rootpath
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

        record: dict[str, Any] = {
            "nodeid": nodeid,
            "outcome": outcome,
            "duration_s": round(duration, 3),
            "log_file": str(log_path.relative_to(self.root)),
            "has_traceback": has_traceback,
        }
        record.update(self._crash_fields(entries, outcome))
        json_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        self._append_index(record)
        return record

    def _crash_fields(
        self,
        entries: list[tuple[pytest.TestReport, str]],
        outcome: str,
    ) -> dict[str, Any]:
        """Structured "what failed, and where" for a finalized test.

        Returns an empty dict for anything that didn't fail, so a passing
        test's record keeps exactly the shape it always had. The phase that
        decided the outcome is the one worth describing: a test whose setup
        errored also emits a teardown report, and the setup traceback is the
        one that explains the failure.
        """
        if outcome not in _CRASHING_OUTCOMES:
            return {}
        culprit = next(
            (report for report, category in entries if category == outcome and report.longreprtext),
            None,
        ) or next((report for report, _category in entries if report.longreprtext), None)
        if culprit is None:
            return {}
        crash = crash_from_report(culprit, self.rootpath)
        if crash is None:
            return {}
        return {"crash": crash, "frames": frames_from_report(culprit, self.rootpath)}

    def add_collect_error(self, report: pytest.CollectReport) -> dict[str, Any]:
        nodeid = report.nodeid or "unknown"
        self.collect_errors_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.collect_errors_dir / f"{_sanitize(nodeid) or 'unknown'}.log"
        log_path.write_text(
            f"nodeid: {nodeid}\noutcome: collect_error\n\n{report.longreprtext}\n",
            encoding="utf-8",
        )
        record: dict[str, Any] = {
            "nodeid": nodeid,
            "outcome": "collect_error",
            "duration_s": 0.0,
            "log_file": str(log_path.relative_to(self.root)),
            "has_traceback": True,
        }
        crash = crash_from_report(report, self.rootpath)
        if crash is not None:
            record["crash"] = crash
            record["frames"] = frames_from_report(report, self.rootpath)
        self._append_index(record)
        return record

    def _append_index(self, record: dict[str, Any]) -> None:
        self._records.append(record)
        with self.index_path.open("a", encoding="utf-8") as index_file:
            index_file.write(json.dumps(record) + "\n")

    def records_with_outcome(self, outcomes: set[str]) -> list[dict[str, Any]]:
        return [record for record in self._records if record["outcome"] in outcomes]
