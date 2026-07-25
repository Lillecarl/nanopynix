from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
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

# How much of one note value goes into index.jsonl, the log, and the terminal.
# The full value is always in notes.jsonl; this only bounds the copies that
# something else has to stay readable. A value big enough to hit this wants
# `attach()`, and the truncation marker says so.
MAX_NOTE_CHARS = 2000

# Where a test's attached files live: a directory beside its .log, named after
# it. Attachments and the log can't collide (`.files` vs `.log`), and one
# directory per test keeps `rm -rf` on a run directory the only cleanup there is.
ATTACHMENT_DIR_SUFFIX = ".files"

_NOTES_FILE = "notes.jsonl"

# Stands in for the nodeid of notes taken while no test was running.
_NO_TEST_LABEL = "(outside any test)"


def sanitize_component(segment: str) -> str:
    """Make *segment* safe to use as a single filesystem path component."""
    return _UNSAFE_PATH_CHARS.sub("_", segment)


def _jsonable(value: object) -> Any:
    """*value* if it survives a JSON round-trip, else its ``repr``.

    A note is troubleshooting output, so an un-serializable value must never
    be the thing that fails the test -- an agent probing a live object should
    get ``repr`` of it, not a TypeError from the probe itself.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value


@dataclass(frozen=True)
class Note:
    """One ``key=value`` an agent asked a test to record.

    Its value is always JSON-safe: :meth:`of` is the only way one is made
    from user input, and it converts.
    """

    key: str
    value: Any

    @classmethod
    def of(cls, key: str, value: object) -> Note:
        return cls(key, _jsonable(value))

    def clipped(self, limit: int = MAX_NOTE_CHARS) -> Any:
        """The value, bounded to *limit* characters, keeping its type if it fits.

        For the copies that share space with something else -- a record, a
        log, a terminal. The full value is always in ``notes.jsonl``.
        """
        text = self.rendered_value(limit=None)
        if len(text) <= limit:
            return self.value
        dropped = len(text) - limit
        return f"{text[:limit]}... [truncated {dropped} chars -- full value in {_NOTES_FILE}, or use attach()]"

    def rendered_value(self, limit: int | None = MAX_NOTE_CHARS) -> str:
        """The value as text: strings as themselves, everything else as JSON."""
        value = self.value if limit is None else self.clipped(limit)
        return value if isinstance(value, str) else json.dumps(value)

    def line(self) -> str:
        """``key=value``, the one format notes are printed in everywhere."""
        return f"{self.key}={self.rendered_value()}"


@dataclass(frozen=True)
class NoteGroup:
    """What one test recorded on purpose, ready to print.

    *label* is its nodeid (or a stand-in, for notes taken while no test was
    running), and *notes* is collapsed to the last value per key.
    """

    label: str
    notes: list[Note]
    attachments: list[str]


@dataclass(frozen=True)
class Phase:
    """One of a test's setup/call/teardown reports, with pytest's category for it."""

    report: pytest.TestReport
    category: str


def _collapse(notes: list[Note]) -> list[Note]:
    """Last value per key, in first-mention order.

    A probe inside a loop reads as "where did it get to" rather than as a
    hundred lines of the same key -- ``notes.jsonl`` and the test's log
    section keep every iteration.
    """
    latest: dict[str, Note] = {}
    for note in notes:
        latest[note.key] = note
    return list(latest.values())


def _captured_sections(phases: list[Phase]) -> list[str]:
    """Everything pytest itself captured, one log section per phase and stream."""
    sections: list[str] = []
    for report in (phase.report for phase in phases):
        if report.longreprtext:
            sections.append(f"=== TRACEBACK ({report.when}) ===\n{report.longreprtext}")
        if report.capstdout:
            sections.append(f"=== STDOUT ({report.when}) ===\n{report.capstdout}")
        if report.capstderr:
            sections.append(f"=== STDERR ({report.when}) ===\n{report.capstderr}")
        caplog = getattr(report, "caplog", "")
        if caplog:
            sections.append(f"=== LOG ({report.when}) ===\n{caplog}")
    return sections


def nodeid_to_relpath(nodeid: str) -> Path:
    """Map a test nodeid to a filesystem-safe relative path, mirroring the
    test file's own path as directories, e.g.
    'tests/test_foo.py::test_bar[a/b]' -> 'tests/test_foo.py/test_bar[a_b]'.
    """
    file_part, _, test_part = nodeid.partition("::")
    test_part = sanitize_component(test_part.replace("::", "__")) or "_module_"
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
        self.notes_path = root / _NOTES_FILE
        self._pending: dict[str, list[Phase]] = {}
        self._records: list[dict[str, Any]] = []
        self._notes: dict[str, list[Note]] = {}
        # Moved here from _notes as each test finishes, so a nodeid that runs
        # twice in one session (--lf in the same process, a rerun plugin)
        # starts collecting from empty rather than inheriting the first run's
        # notes. Kept for the whole session because the end-of-run summary
        # needs them; it is only ever what tests deliberately recorded.
        self._finalized_notes: dict[str, list[Note]] = {}
        self._loose_notes: list[Note] = []
        self._notes_lock = threading.Lock()
        self._started_at = 0.0

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text("", encoding="utf-8")
        self._started_at = time.monotonic()

    def add_note(self, nodeid: str | None, key: str, value: object) -> Path:
        """Record one note, appending it to ``notes.jsonl`` before returning.

        Written to disk on every call, not buffered until the test finishes:
        the reason to add a probe in the first place is that something is
        going wrong, and a segfault, ``os._exit``, or a killed hang never
        reaches the end of the test. Appending as we go means the notes from
        the run that died are still there to read afterwards. (No fsync: a
        closed file survives any process death; only losing the machine
        itself could drop it, and then the run is gone anyway.)
        """
        note = Note.of(key, value)
        line = json.dumps(
            {
                "t": round(time.monotonic() - self._started_at, 3),
                "nodeid": nodeid,
                "key": note.key,
                "value": note.value,
            },
        )
        with self._notes_lock:
            if nodeid is None:
                self._loose_notes.append(note)
            else:
                self._notes.setdefault(nodeid, []).append(note)
            self.root.mkdir(parents=True, exist_ok=True)
            with self.notes_path.open("a", encoding="utf-8") as notes_file:
                notes_file.write(line + "\n")
        return self.notes_path

    def attachment_dir_for(self, nodeid: str) -> Path:
        """The directory for *nodeid*'s attached files, created on demand."""
        rel = nodeid_to_relpath(nodeid)
        directory = self.root / rel.parent / f"{rel.name}{ATTACHMENT_DIR_SUFFIX}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def note_groups(self) -> list[NoteGroup]:
        """Everything this run's tests recorded on purpose, in run order.

        Built for the end-of-run summary, so each group's notes are collapsed
        to the last value per key.
        """
        groups = [
            NoteGroup(
                label=record["nodeid"],
                notes=_collapse(self._finalized_notes.get(record["nodeid"], [])),
                attachments=record.get("attachments") or [],
            )
            for record in self._records
            if self._finalized_notes.get(record["nodeid"]) or record.get("attachments")
        ]
        if self._loose_notes:
            groups.append(NoteGroup(_NO_TEST_LABEL, _collapse(self._loose_notes), []))
        return groups

    def add_report(self, report: pytest.TestReport, category: str) -> dict[str, Any] | None:
        """Feed one setup/call/teardown phase report. Returns the finalized
        record once the teardown phase (always the last phase pytest emits
        for a given test, whether or not setup/call succeeded) arrives.
        """
        phases = self._pending.setdefault(report.nodeid, [])
        phases.append(Phase(report, category))
        if report.when != "teardown":
            return None
        del self._pending[report.nodeid]
        return self._finalize(report.nodeid, phases)

    def _finalize(self, nodeid: str, phases: list[Phase]) -> dict[str, Any]:
        categories = {phase.category for phase in phases if phase.category}
        outcome = next((c for c in _CATEGORY_PRECEDENCE if c in categories), "passed")
        duration = sum(phase.report.duration for phase in phases)

        rel = nodeid_to_relpath(nodeid)
        with self._notes_lock:
            notes = self._notes.pop(nodeid, [])
            if notes:
                self._finalized_notes[nodeid] = notes
        attachments = self._attachments_for(rel)

        sections = [f"nodeid: {nodeid}", f"outcome: {outcome}", f"duration_s: {duration:.3f}"]
        # Notes and attachments go above the traceback: they are the one part
        # of this file somebody deliberately put here, so they shouldn't be
        # below however many hundred lines of captured output.
        if notes:
            sections.append("=== NOTES ===\n" + "\n".join(note.line() for note in notes))
        if attachments:
            sections.append("=== ATTACHMENTS ===\n" + "\n".join(attachments))
        sections += _captured_sections(phases)
        has_traceback = any(phase.report.longreprtext for phase in phases)

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
        if notes:
            record["notes"] = {note.key: note.clipped() for note in _collapse(notes)}
        if attachments:
            record["attachments"] = attachments
        record.update(self._crash_fields(phases, outcome))
        json_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        self._append_index(record)
        return record

    def _attachments_for(self, rel: Path) -> list[str]:
        """Every file the test attached, as paths relative to the run directory.

        Found by listing the directory rather than by remembering what
        ``attach()`` wrote, so a file the test dropped in ``agent_notes.dir``
        itself -- a subprocess's output, a dumped payload -- is listed too.
        """
        directory = self.root / rel.parent / f"{rel.name}{ATTACHMENT_DIR_SUFFIX}"
        if not directory.is_dir():
            return []
        return sorted(str(path.relative_to(self.root)) for path in directory.rglob("*") if path.is_file())

    def _crash_fields(self, phases: list[Phase], outcome: str) -> dict[str, Any]:
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
            (p.report for p in phases if p.category == outcome and p.report.longreprtext),
            None,
        ) or next((p.report for p in phases if p.report.longreprtext), None)
        if culprit is None:
            return {}
        crash = crash_from_report(culprit, self.rootpath)
        if crash is None:
            return {}
        return {"crash": crash, "frames": frames_from_report(culprit, self.rootpath)}

    def add_collect_error(self, report: pytest.CollectReport) -> dict[str, Any]:
        nodeid = report.nodeid or "unknown"
        self.collect_errors_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.collect_errors_dir / f"{sanitize_component(nodeid) or 'unknown'}.log"
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
