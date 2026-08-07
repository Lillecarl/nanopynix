from __future__ import annotations

import hashlib
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

# Bytes a single path component may take. NAME_MAX is 255 on every filesystem
# this is likely to meet (ext4, xfs, btrfs, APFS); the slack is for the
# longest suffix appended to a test's name -- ".stuck.txt".
MAX_NAME_BYTES = 245
_NAME_HASH_CHARS = 8

# Characters of a nodeid that may reach a terminal line. Sized above the long
# tail of real ids rather than to make normal output shorter -- in the suite
# this plugin was written against, ids run to a median of 88 characters and a
# 95th percentile of 119, so this leaves ordinary output untouched and bounds
# only the pathological case.
MAX_NODEID_DISPLAY_CHARS = 140
_ELLIPSIS = "..."

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


def _write_text(path: Path, text: str, *, make_parent: bool = False) -> str | None:
    """Write *text* to *path*; return why not, rather than raising.

    Recording one test's detail must never be able to end the session. It
    runs inside pytest_runtest_logreport, where an exception is an
    INTERNALERROR that abandons every test after it -- so a filesystem this
    plugin merely happens to dislike (a name over NAME_MAX, a full disk, a
    read-only mount) would cost the whole run rather than one log file. The
    reason goes into the test's index.jsonl record as `capture_error`, where
    it is visible without being fatal.
    """
    try:
        if make_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as error:
        return f"{type(error).__name__}: {error}"
    return None


def fit_component(name: str) -> str:
    """Shorten *name* to something a filesystem will actually accept.

    A parametrized id is only bounded by whatever the test passed to
    ``@pytest.mark.parametrize`` -- a long string, a serialized payload -- and
    a component over NAME_MAX makes every write for that test fail with
    ENAMETOOLONG. Before this, one such test aborted the whole session with an
    INTERNALERROR and no results at all, in agent mode only.

    Kept: the head, which holds the test function name and the start of the
    id, and a hash of the whole thing so two ids sharing a long prefix don't
    collide. The nodeid is never recovered from a name like this, which
    nodeid_is_evident_from already detects, so the queries print it alongside.
    """
    encoded = name.encode("utf-8")
    if len(encoded) <= MAX_NAME_BYTES:
        return name
    digest = hashlib.sha256(encoded).hexdigest()[:_NAME_HASH_CHARS]
    # errors="ignore" because a byte-wise cut can land inside a multi-byte
    # character; dropping that partial character is the whole handling needed.
    head = encoded[: MAX_NAME_BYTES - _NAME_HASH_CHARS - 1].decode("utf-8", errors="ignore")
    return f"{head}~{digest}"


def safe_component(name: str) -> str:
    """Turn one test's name into a filename that is unique to it.

    Sanitizing alone is not injective: ``test_p[a/b]`` and ``test_p[a_b]`` are
    two different tests that both sanitize to ``test_p[a_b]``, so the second
    to finish silently overwrote the first's log -- and `show` would then
    print a passing test's detail for a failing one, which is worse than
    printing nothing. A name that had to be changed carries a hash of what it
    was, so it can only stand for that test.
    """
    sanitized = sanitize_component(name)
    if sanitized != name:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_NAME_HASH_CHARS]
        sanitized = f"{sanitized}~{digest}"
    return fit_component(sanitized)


def nodeid_to_relpath(nodeid: str) -> Path:
    """Map a test nodeid to a filesystem-safe relative path, mirroring the
    test file's own path as directories, e.g.
    'tests/test_foo.py::test_bar[a-b]' -> 'tests/test_foo.py/test_bar[a-b]'.

    A name that survives unchanged reads back as its nodeid, which is what
    lets the queries print one path instead of a path and an id; one that had
    to be sanitized or shortened does not, and nodeid_is_evident_from detects
    exactly that.
    """
    file_part, _, test_part = nodeid.partition("::")
    test_part = safe_component(test_part.replace("::", "__")) or "_module_"
    return Path(file_part) / test_part


def stuck_dump_path(root: Path, nodeid: str) -> Path:
    """Where stack dumps for *nodeid* go: beside its log, not inside it.

    The log is written when a test finishes, and a test being dumped may
    never finish.

    Here rather than on AgentRuntime, which is what writes these files,
    because `pytest-agent watch` reads them from another process entirely. A
    naming convention that one side computes and the other side re-derives is
    a convention that drifts.
    """
    rel = nodeid_to_relpath(nodeid)
    return root / rel.parent / f"{rel.name}.stuck.txt"


def abbreviate_nodeid(nodeid: str, limit: int = MAX_NODEID_DISPLAY_CHARS) -> str:
    """Shorten *nodeid* to at most *limit* characters for a terminal line.

    Only for display. Everything written to a file keeps the full id, so the
    queries and the records are unaffected -- this exists because the progress
    line reprints the running nodeid on every heartbeat, and a hung test with
    a long parametrized id would otherwise repeat a several-hundred-character
    line until the run is killed.

    The middle goes rather than the tail: the file path at the front and the
    parameters at the back are what tell two ids apart, and dropping the tail
    would collapse a whole parametrized family to one indistinguishable line.
    """
    if len(nodeid) <= limit:
        return nodeid
    keep = limit - len(_ELLIPSIS)
    head = keep - keep // 3
    tail = keep - head
    return f"{nodeid[:head]}{_ELLIPSIS}{nodeid[len(nodeid) - tail :]}"


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
        # Set if appending to index.jsonl ever failed, so the run can say so
        # once at the end instead of once per test.
        self.index_error: str | None = None
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
        # Tolerant like _append_index: if the archive can't be opened, the run
        # still runs and still reports to the terminal from memory.
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.index_path.write_text("", encoding="utf-8")
        except OSError as error:
            self.index_error = f"{type(error).__name__}: {error}"
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
        log_path = out_dir / f"{rel.name}.log"
        json_path = out_dir / f"{rel.name}.json"
        capture_error = _write_text(log_path, "\n\n".join(sections) + "\n", make_parent=True)

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
        if capture_error is None:
            capture_error = _write_text(json_path, json.dumps(record, indent=2) + "\n")
        if capture_error is not None:
            record["capture_error"] = capture_error
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
        """Append one record to index.jsonl, keeping it in memory regardless.

        Tolerant for the same reason as _write_text: this runs inside
        pytest_runtest_logreport, so raising here abandons every test after
        it. A run whose index can't be written has lost its archive, but the
        in-memory records still drive the closing block, so an agent watching
        the terminal is told what failed even then. index_error is reported
        once, by the runtime, rather than once per test.
        """
        self._records.append(record)
        try:
            with self.index_path.open("a", encoding="utf-8") as index_file:
                index_file.write(json.dumps(record) + "\n")
        except OSError as error:
            if self.index_error is None:
                self.index_error = f"{type(error).__name__}: {error}"

    def records_with_outcome(self, outcomes: set[str]) -> list[dict[str, Any]]:
        return [record for record in self._records if record["outcome"] in outcomes]

    def records_with_capture_error(self) -> list[dict[str, Any]]:
        """Tests whose detail could not be written. Normally none."""
        return [record for record in self._records if record.get("capture_error")]
