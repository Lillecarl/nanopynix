"""`pytest-agent watch` -- follow a run in progress and report what it does.

The other query commands answer a question about a run that has already
happened. This one answers "tell me when something happens", which is what a
long suite started in the background actually needs: without it the caller
has to guess when to look, and every guess is either too early or too late.

**One line of stdout per event, and the command ends by itself.** That shape
is not decoration. An agent harness turns each line into a notification, so a
line has to be worth interrupting somebody for, and a command that never
exits stays armed long after the thing it watched is over.

Four events, and the fourth is the one that makes the other three
trustworthy:

* ``FAIL`` / ``ERROR`` -- a test finished badly, named with its crash message.
* ``STUCK`` -- one test has been running implausibly long.
* ``DONE`` -- the run finished, with its counts and exit status.
* ``DIED`` -- the process is gone and wrote no summary.

Without ``DIED`` a crashed run and a healthy one are both silence, so silence
would mean nothing. With it, silence means the run is fine.

Nothing here parses terminal output. Every event comes from a file the run
writes as it goes -- ``index.jsonl`` for finished tests, ``status.json`` for
the test running now, ``summary.json`` for the end -- which is why this
watcher does not care how the run was started or where its terminal went.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pytest_agent._capture import stuck_dump_path
from pytest_agent._history import existing_run_numbers, run_is_locked, run_label
from pytest_agent._paths import display_path
from pytest_agent._records import FAILING_OUTCOMES, QueryError, crash_of, one_line, resolve_agent_dir

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterable, Iterator
    from typing import TextIO

WATCH_COMMAND = "watch"

# When a running test is called stuck. Deliberately separate from the run's
# own `--agent-stuck-after` (300s), which does a different job: that one dumps
# every thread's stack into the run directory, and this one interrupts a
# person. Noticing should come first, so this default is lower -- and when a
# dump does exist, the STUCK line names it.
DEFAULT_STUCK_AFTER = 120.0

# How often the files are re-read. Matches the run's default status-file
# interval; polling faster only re-reads the same bytes.
DEFAULT_POLL = 2.0

# How long to wait for the run to appear before giving up. The gap between
# starting a suite in the background and arming a watcher on it is seconds,
# but a cold `nix develop` in front of pytest can make it much more than that.
DEFAULT_WAIT = 60.0

# Failures reported one line each. Past this the rest are counted and the
# totals arrive with DONE.
#
# The cap protects the DONE line, which is the one that matters most: an agent
# harness stops a watch that produces too many notifications, and a suite with
# 200 failures would trip that and lose the end of the run. It also matches
# what a reader can use -- past ten failures the question stops being "which
# ones" and becomes "what happened", which `pytest-agent digest` answers.
MAX_REPORTED_FAILURES = 10

# Stuck notices for one test, at a doubling interval: 120s, 240s, 480s, 960s.
# A test that is wedged says so once and then stays quiet, rather than filing
# a notification on every poll for as long as the run lasts.
MAX_STUCK_NOTICES = 4

# How long a label that matches only *finished* runs is held before one of
# them is accepted.
#
# The hazard it covers: labels are meant to be reused, so `--run nightly`
# regularly matches both last night's run and tonight's, and a watcher armed
# in the seconds before tonight's claims its directory would attach to last
# night's and report it finished at once.
#
# Bounded by a few seconds rather than by --wait, which was the first attempt
# and was worse than the problem: a suite that finishes before its watcher
# attaches -- an ordinary thing for a fast one -- then blocked for a minute
# before saying so. Measured, by breaking two tests that do exactly that.
#
# Past this grace the behaviour is what it was before the guard existed, so
# the worst case is no worse than not having it.
FINISHED_MATCH_GRACE = 5.0

# How stale status.json may be before a run with no readable pid is presumed
# dead. Generously above the default write interval, because a loaded machine
# can delay a thread by seconds.
STATUS_GRACE = 30.0

EXIT_CLEAN = 0
EXIT_FAILED_TO_WATCH = 1
EXIT_RUN_FAILED = 2
EXIT_RUN_DIED = 3

# Padded so the events line up in a terminal, and so a reader can tell them
# apart by shape before reading the words.
_LABEL_WIDTH = 5


@dataclass(frozen=True)
class WatchOptions:
    """What the caller asked for, separated from what has happened so far."""

    stuck_after: float = DEFAULT_STUCK_AFTER
    poll: float = DEFAULT_POLL
    wait: float = DEFAULT_WAIT


@dataclass
class WatchState:
    """Everything the watcher remembers between two polls.

    Held in one object, and passed to `poll_once` with a clock, so that every
    event this command can produce is reachable from a test that writes a run
    directory by hand. A watcher whose only test is a real pytest run is a
    watcher whose failure paths -- a dead process, a corrupt line, a flood --
    are never exercised, because those are exactly the runs that are hard to
    stage on purpose.
    """

    offset: int = 0
    partial_line: str = ""
    failures_seen: int = 0
    failures_reported: int = 0
    flood_reported: bool = False
    corruption_reported: bool = False
    stuck_notices: dict[str, int] = field(default_factory=dict[str, int])
    finished: bool = False
    died: bool = False
    exit_code: int = EXIT_CLEAN

    @property
    def ended(self) -> bool:
        return self.finished or self.died


@dataclass(frozen=True)
class Status:
    """``status.json``, re-validated field by field."""

    written_at: float
    running: str | None
    running_since_s: float
    distributed: bool


def add_watch_arguments(parser: argparse.ArgumentParser) -> None:
    """The options that belong to `watch` alone; --run and --dir are shared."""
    parser.add_argument(
        "--stuck-after",
        type=float,
        default=DEFAULT_STUCK_AFTER,
        metavar="SECONDS",
        help=(
            "Report a test that has been running this long, and again at each "
            "doubling (default: %(default)s; 0 reports none). Separate from the "
            "run's own --agent-stuck-after, which dumps stacks rather than reporting."
        ),
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=DEFAULT_POLL,
        metavar="SECONDS",
        help="Seconds between re-reads of the run's files (default: %(default)s).",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_WAIT,
        metavar="SECONDS",
        help=(
            "Seconds to wait for the run to appear before giving up, so a watcher "
            "may be armed before the run starts (default: %(default)s)."
        ),
    )


def watch(
    explicit_dir: str | None,
    run: str | None,
    options: WatchOptions,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Follow one run and print its events. Returns a process exit code.

    `out` and `err` are injectable so a test can read the events without a
    subprocess. They default late rather than in the signature, because
    `pytest`'s capture replaces `sys.stdout` after this module is imported.
    """
    stream = sys.stdout if out is None else out
    errors = sys.stderr if err is None else err
    run_dir = _attach(explicit_dir, run, options)
    # On stderr, so it reaches the log without becoming an event. The events
    # of this command are things that happened to the run; "I am watching" is
    # a thing that happened to the watcher.
    print(f"pytest-agent: watching {display_path(run_dir)}", file=errors, flush=True)

    state = WatchState()
    while True:
        for line in poll_once(run_dir, state, time.time(), options):
            print(line, file=stream, flush=True)
        if state.ended:
            return state.exit_code
        time.sleep(options.poll)


def poll_once(run_dir: Path, state: WatchState, now: float, options: WatchOptions) -> list[str]:
    """The events that have become true since the last call, in order.

    Finished tests come before the end of the run, so a failure is never
    reported after the DONE line that counts it. The end of the run comes
    before the death of the run, so a process that exits the moment it
    finishes is reported as finished -- which it is.
    """
    events = list(_finished_test_events(run_dir, state))
    status = _read_status(run_dir)
    events.extend(_stuck_events(run_dir, state, status, now, options))

    summary = _read_json(run_dir / "summary.json")
    if summary is not None:
        state.finished = True
        state.exit_code = EXIT_RUN_FAILED if _run_went_badly(summary) else EXIT_CLEAN
        events.append(_done_line(run_dir, summary))
        return events

    if not _looks_alive(run_dir, status, now):
        state.died = True
        state.exit_code = EXIT_RUN_DIED
        events.append(_died_line(run_dir, state, status))
    return events


def _finished_test_events(run_dir: Path, state: WatchState) -> Iterator[str]:
    for record in _read_new_records(run_dir / "index.jsonl", state):
        if record is None:
            if not state.corruption_reported:
                state.corruption_reported = True
                yield _event(
                    "WARN",
                    f"{display_path(run_dir / 'index.jsonl')} holds a line that is not JSON -- skipping it",
                )
            continue
        outcome = record.get("outcome")
        if outcome not in FAILING_OUTCOMES:
            continue
        state.failures_seen += 1
        if state.failures_reported >= MAX_REPORTED_FAILURES:
            if not state.flood_reported:
                state.flood_reported = True
                yield _event(
                    "MORE",
                    f"{MAX_REPORTED_FAILURES} failures reported; the rest are counted, not listed -- "
                    "the totals come with DONE",
                )
            continue
        state.failures_reported += 1
        yield _failure_line(record, str(outcome))


def _failure_line(record: dict[str, Any], outcome: str) -> str:
    crash = crash_of(record)
    # Not "no crash recorded": a run from an older pytest-agent has no crash
    # field at all, and saying the failure has no cause would be a lie about
    # the test rather than about the record.
    message = one_line(crash.message) if crash is not None else "no crash message in this record"
    # `error` and `collect_error` share a label. Both mean the test never got
    # to say anything about itself, which is the distinction that matters when
    # deciding whether to go and read it.
    label = "FAIL" if outcome == "failed" else "ERROR"
    return _event(label, f"{record.get('nodeid', '?')} -- {message}")


def _stuck_events(
    run_dir: Path,
    state: WatchState,
    status: Status | None,
    now: float,
    options: WatchOptions,
) -> Iterator[str]:
    if status is None or status.running is None or options.stuck_after <= 0:
        return
    # Under xdist several tests run at once and `running` names an arbitrary
    # one of them, so its age says nothing about whether anything is wedged.
    # Reporting one would be a guess dressed as a measurement.
    if status.distributed:
        return
    nodeid = status.running
    # The file was written at some point in the past, so the age it recorded
    # has grown by however long ago that was. max(): a clock that stepped
    # backwards must not make a running test look younger than its own record.
    age = status.running_since_s + max(0.0, now - status.written_at)
    notices = state.stuck_notices.get(nodeid, 0)
    if notices >= MAX_STUCK_NOTICES or age < options.stuck_after * (2**notices):
        return
    state.stuck_notices[nodeid] = notices + 1
    dump = stuck_dump_path(run_dir, nodeid)
    where = f" -- stack: {display_path(dump)}" if dump.is_file() else ""
    yield _event("STUCK", f"{age:.0f}s {nodeid}{where}")


def _done_line(run_dir: Path, summary: dict[str, Any]) -> str:
    counts = summary.get("counts")
    counts = cast("dict[str, Any]", counts) if isinstance(counts, dict) else {}
    failed = sum(_count(counts, name) for name in FAILING_OUTCOMES)
    parts = [
        f"{failed} failed",
        f"{_count(counts, 'passed')} passed",
        f"{_count(counts, 'skipped')} skipped",
    ]
    duration = summary.get("duration_s")
    took = f" in {duration:.0f}s" if isinstance(duration, (int, float)) else ""
    exit_status = summary.get("exit_status")
    status = f" (exit {exit_status})" if isinstance(exit_status, int) else ""
    line = f"{_named(run_dir)}: {', '.join(parts)}{took}{status}"

    killed_by = summary.get("killed_by")
    if isinstance(killed_by, str) and killed_by:
        interrupted = summary.get("interrupted_at")
        during = f" while running {interrupted}" if isinstance(interrupted, str) and interrupted else ""
        line += f" -- interrupted by {killed_by}{during}"
    if failed:
        # The next command, spelled out with the selector that reaches this
        # run. A reader who has just been told about four failures should not
        # have to work out how to name the run they landed in.
        line += f" -- pytest-agent digest {_selector_of(run_dir)}"
    return _event("DONE", line)


def _died_line(run_dir: Path, state: WatchState, status: Status | None) -> str:
    """What is left to say about a run that stopped without finishing.

    The test it was on is the whole content of the message when there is one:
    that test never finished, so it has no record and no log, and the counts
    say only that it is not there.
    """
    running = "" if status is None or status.running is None else f", last running {status.running}"
    return _event(
        "DIED",
        f"{_named(run_dir)}: the process is gone and wrote no summary -- "
        f"{state.failures_seen} failures seen so far{running}. Its records stop where it stopped: "
        f"pytest-agent last-failures {_selector_of(run_dir)}",
    )


def _named(run_dir: Path) -> str:
    label = run_label(run_dir)
    return run_dir.name if label is None else f"{run_dir.name} [{label}]"


def _selector_of(run_dir: Path) -> str:
    """How to name this run to another subcommand.

    The label when there is one, because that is what the caller chose and
    what they will still recognise; the number otherwise.
    """
    label = run_label(run_dir)
    if label is not None:
        return f"--run {label}"
    return f"--run {run_dir.name.removeprefix('runs-').lstrip('0') or '0'}"


def _event(label: str, text: str) -> str:
    return f"{label:<{_LABEL_WIDTH}} {text}"


def _count(counts: dict[str, Any], name: str) -> int:
    value = counts.get(name)
    return value if isinstance(value, int) else 0


def _run_went_badly(summary: dict[str, Any]) -> bool:
    counts = summary.get("counts")
    counts = cast("dict[str, Any]", counts) if isinstance(counts, dict) else {}
    if any(_count(counts, name) for name in FAILING_OUTCOMES):
        return True
    # A run can exit non-zero with no failing test: no tests collected, a
    # usage error, an interrupt. The caller asked to be told how the run went,
    # and "0 failures" would not be that.
    exit_status = summary.get("exit_status")
    return isinstance(exit_status, int) and exit_status != 0


def _looks_alive(run_dir: Path, status: Status | None, now: float) -> bool:
    """Whether the process that owns *run_dir* is still there.

    The pid is the answer when there is one, because it is definitive: a run
    killed by SIGKILL, or one that segfaulted, is gone from the process table
    the moment it dies, while every file it left behind still looks exactly
    as it did.

    Staleness of status.json is the fallback, for a run whose meta.json never
    got written. It cannot be the primary test, because a run started with
    --agent-status-interval 0 writes that file once and never again, and
    presuming such a run dead thirty seconds in would be wrong every time.

    The limit worth knowing: a pid means nothing across a pid namespace or a
    machine boundary, and a recycled pid reads as alive. Both make this
    over-report life, never death -- the watcher then waits instead of
    announcing a death that did not happen.
    """
    pid = _pid_of(run_dir)
    if pid is not None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # EPERM: the process exists and belongs to somebody else. Any
            # other errno is a question this cannot answer, and "alive" is the
            # answer that waits rather than the one that announces.
            return True
        return True
    if status is None:
        # Nothing to go on at all -- no pid, no status. The run may be in the
        # moment between claiming its directory and writing either file.
        return True
    return now - status.written_at <= STATUS_GRACE


def _pid_of(run_dir: Path) -> int | None:
    meta = _read_json(run_dir / "meta.json")
    if meta is None:
        return None
    pid = meta.get("pid")
    return pid if isinstance(pid, int) and pid > 0 else None


def _read_status(run_dir: Path) -> Status | None:
    raw = _read_json(run_dir / "status.json")
    if raw is None:
        return None
    written_at = raw.get("written_at")
    if not isinstance(written_at, (int, float)):
        return None
    running = raw.get("running")
    since = raw.get("running_since_s")
    return Status(
        written_at=float(written_at),
        running=running if isinstance(running, str) and running else None,
        running_since_s=float(since) if isinstance(since, (int, float)) else 0.0,
        distributed=bool(raw.get("distributed")),
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    """One JSON object off disk, or None for anything that is not one.

    Total on purpose. Every file this reads is being written by another
    process, so "absent", "half written" and "written by a newer version" are
    all ordinary, and none of them is worth ending a watch over.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        loaded: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else None


def _read_new_records(index_path: Path, state: WatchState) -> Iterator[dict[str, Any] | None]:
    """Records appended since the last poll. ``None`` marks an unreadable line.

    Read by byte offset rather than by re-parsing the file, so the cost of a
    poll is what arrived rather than what has accumulated -- a suite with
    three thousand tests would otherwise re-read and re-parse every record
    every two seconds for the length of the run.

    A trailing fragment is kept rather than parsed. The run appends a record
    as each test finishes, so the last line is routinely half-written at the
    moment this reads, and treating that as corruption would report a failure
    that had not happened yet.
    """
    try:
        size = index_path.stat().st_size
    except OSError:
        return
    if size < state.offset:
        # The file got shorter, which an append-only file cannot do. Something
        # replaced it; start again rather than read from an offset that now
        # points into the middle of a different record.
        state.offset = 0
        state.partial_line = ""
    if size == state.offset:
        return
    try:
        with index_path.open("rb") as handle:
            handle.seek(state.offset)
            chunk = handle.read(size - state.offset)
    except OSError:
        return
    state.offset += len(chunk)
    lines = (state.partial_line + chunk.decode("utf-8", errors="replace")).split("\n")
    state.partial_line = lines.pop()
    for line in lines:
        if not line.strip():
            continue
        try:
            record: object = json.loads(line)
        except json.JSONDecodeError:
            yield None
            continue
        if isinstance(record, dict):
            yield cast("dict[str, Any]", record)


def _attach(explicit_dir: str | None, selector: str | None, options: WatchOptions) -> Path:
    """The run to follow, waiting up to ``options.wait`` for it to exist.

    Waiting is what makes "start the suite, then arm the watcher" a sequence
    rather than a race: the run claims its directory inside pytest_configure,
    which is after the interpreter, the plugins and the conftest files have
    all loaded.

    **The wait covers the agent directory as well as the run inside it.** The
    first run in a fresh checkout creates that directory, so resolving it once
    up front refused exactly the case this command is for -- measured, by
    arming a watcher on a directory the run had not created yet.
    """
    wait = max(options.wait, 0.0)
    started = time.monotonic()
    deadline = started + wait
    # A run that is going always wins, on every poll. A run of that name that
    # has already finished is taken only once the grace is over, which gives
    # a run being started right now the few seconds it needs to claim its
    # directory.
    accept_finished_from = started + min(wait, FINISHED_MATCH_GRACE)
    while True:
        agent_dir: Path | None = None
        missing_dir: QueryError | None = None
        try:
            agent_dir = resolve_agent_dir(explicit_dir)
        except QueryError as error:
            missing_dir = error
        now = time.monotonic()
        if agent_dir is not None:
            found = _find_run(agent_dir, selector, accept_finished=now >= accept_finished_from)
            if found is not None:
                return found
        remaining = deadline - now
        if remaining <= 0:
            if agent_dir is None and missing_dir is not None:
                raise QueryError(f"{missing_dir}; waited {options.wait:.0f}s for it to appear")
            raise QueryError(_nothing_to_watch(cast("Path", agent_dir), selector, options.wait))
        time.sleep(min(options.poll, remaining))


def _nothing_to_watch(agent_dir: Path, selector: str | None, waited: float) -> str:
    where = display_path(agent_dir)
    if selector is None:
        return (
            f"no run was in progress under {where}, and none started within {waited:.0f}s -- "
            "start one with `pytest --agent`, or follow a particular run with --run N|LABEL"
        )
    return (
        f"no run named {selector!r} appeared under {where} within {waited:.0f}s -- "
        f"a run answers to that name once it starts as `pytest --agent-label {selector}`, "
        "and --wait allows longer"
    )


def _find_run(agent_dir: Path, selector: str | None, *, accept_finished: bool) -> Path | None:
    """The run *selector* names, or the newest live one when it names nothing.

    "Live" rather than "newest", and that is the whole of the difference
    between this and how every other subcommand resolves a run. The others
    answer about a run that is over, so the newest is the right default. This
    one follows a run that is going, and defaulting to the newest would
    quietly attach to the suite before this one and report it finished --
    which is a wrong answer that looks exactly like a right one.

    **A label needs the same care, and for a sharper reason: labels are meant
    to be reused.** Re-running one command with one name is the documented
    way to use them, so `--run nightly` regularly matches both last night's
    finished run and tonight's. Preferring the locked one settles that. A name
    that matches only finished runs is accepted at the deadline instead
    (`accept_finished`), so watching a run that is already over still works
    -- it just cannot win a race against the run being started right now.

    A numeric selector is exempt: `--run 42` names one directory and can mean
    nothing else, so there is no race to lose.
    """
    if selector is None:
        return _newest_live(run_dir for _, run_dir in _runs_by_number(agent_dir))
    if selector.isdigit():
        candidate = agent_dir / f"runs-{int(selector):04d}"
        return candidate if candidate.is_dir() else None
    # Exact match, like `--run LABEL` everywhere else: a label is a name the
    # caller chose, and having `full` answer for `full-suite-2` would make it
    # less trustworthy than the run number it replaces.
    matching = [run_dir for _, run_dir in _runs_by_number(agent_dir) if run_label(run_dir) == selector]
    live = _newest_live(matching)
    if live is not None:
        return live
    return matching[-1] if matching and accept_finished else None


def _newest_live(candidates: Iterable[Path]) -> Path | None:
    """The last of *candidates*, oldest first, that a session still holds."""
    now = time.time()
    live = [run_dir for run_dir in candidates if run_is_locked(run_dir, now)]
    return live[-1] if live else None


def _runs_by_number(agent_dir: Path) -> list[tuple[int, Path]]:
    """Every run directory, oldest first."""
    try:
        numbers = sorted(existing_run_numbers(agent_dir))
    except OSError:
        return []
    return [(number, agent_dir / f"runs-{number:04d}") for number in numbers]
