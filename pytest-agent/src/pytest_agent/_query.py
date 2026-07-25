"""`pytest-agent show|last-failures|digest|history|compare` -- read-only queries
over what previous agent-mode runs wrote to disk.

The point of these is that reading one failure should not require the agent
to *construct* anything. Before them, getting at a single test's detail meant
knowing the run number, mirroring the test file's path into the run
directory, and shell-quoting the brackets in a parametrized id -- three
independent chances to get it wrong, each costing a whole turn.

`history` and `compare` read across runs rather than within one, for the
question the others can't answer: was this failing before I touched it? An
agent that can't check that either claims "pre-existing" without evidence or
spends two turns re-running an old revision to find out.

Everything here reads ``index.jsonl`` and the per-test ``.log`` files. No
pytest session is started, and in particular nothing routes through
``pytest.main()``: the output of a query *is* the thing an agent will
reasonably want to pipe into ``grep``, and running it through pytest would
put it behind the piped-stdout guard for no reason.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pytest_agent._capture import nodeid_is_evident_from
from pytest_agent._crash import normalize_message
from pytest_agent._history import existing_run_numbers, run_is_locked
from pytest_agent._paths import display_path

if TYPE_CHECKING:
    from collections.abc import Sequence

# argv[0] values that mean "query", not "run pytest". Everything else the
# console script sees is forwarded to pytest untouched.
SUBCOMMANDS = ("show", "last-failures", "digest", "history", "compare", "help")

FAILING_OUTCOMES = frozenset({"failed", "error", "collect_error"})

# Nodeids listed in full, per digest group and per compare/history section,
# before collapsing into a count. These commands answer "what shape is this
# run in?" -- an exhaustive nodeid list works against that.
MAX_LISTED_NODEIDS = 10

# A crash message under a list entry is a label, not the failure itself:
# `show` prints the whole thing.
MAX_MESSAGE_CHARS = 160

_NO_CRASH_GROUP = "\x00no-crash"

_EPILOG = """\
`pytest-agent rerun [--run N] [pytest args...]` re-runs the tests that failed
in a recorded run.

Anything else is forwarded to pytest with --agent, so `pytest-agent -x tests/`
is `pytest --agent -x tests/`. Only the exact words above are subcommands; a
path that collides with one still works as `pytest-agent ./show`.
"""


class QueryError(Exception):
    """A query that could not be answered -- reported to stderr, exit code 1."""


@dataclass(frozen=True)
class Crash:
    """What failed, as recorded by _crash.crash_from_report."""

    message: str
    location: str | None


@dataclass(frozen=True)
class Frame:
    """One traceback file location, as recorded by _crash.frames_from_report."""

    path: str
    lineno: int | None
    first_party: bool


@dataclass(frozen=True)
class RunResult:
    """One run's records, kept with the run it came from."""

    number: int
    run_dir: Path
    by_nodeid: dict[str, dict[str, Any]]

    def outcome_of(self, nodeid: str) -> str | None:
        record = self.by_nodeid.get(nodeid)
        return str(record["outcome"]) if record is not None else None

    def failed(self, nodeid: str) -> bool:
        return self.outcome_of(nodeid) in FAILING_OUTCOMES


@dataclass
class FailureGroup:
    """Failures sharing one normalized crash message."""

    message: str
    location: str | None
    frames: list[Frame]
    frames_are_first_party: bool
    # default_factory=list[str] rather than plain `list`: the parametrized
    # alias is callable and gives the checker the element type, which a bare
    # `list` leaves unknown under strict mode.
    nodeids: list[str] = field(default_factory=list[str])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pytest-agent",
        description="Query the per-test detail written by a previous `pytest --agent` run.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Split in two because the cross-run subcommands span every run on disk:
    # --run has no meaning for them, and offering it would only invite the
    # question of what `history --run 3` could possibly mean.
    where = argparse.ArgumentParser(add_help=False)
    where.add_argument(
        "--dir",
        default=None,
        metavar="PATH",
        help="Agent directory to read (default: the nearest .pytest-agent at or above the cwd).",
    )
    common = argparse.ArgumentParser(add_help=False, parents=[where])
    common.add_argument(
        "--run",
        type=int,
        default=None,
        metavar="N",
        help="Query run N instead of the most recent one.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser(
        "show",
        parents=[common],
        help="Print one test's full detail, found by nodeid or any unique substring of one.",
    )
    show.add_argument("pattern", help="A full nodeid, or any substring that matches exactly one.")
    show.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Print every matching test instead of refusing an ambiguous pattern.",
    )

    last_failures = subparsers.add_parser(
        "last-failures",
        parents=[common],
        help="List the run's failing tests, each with its resolved log path.",
    )
    last_failures.add_argument(
        "--detail",
        action="store_true",
        help="Inline each failure's full detail instead of just naming its log file.",
    )

    subparsers.add_parser(
        "digest",
        parents=[common],
        help="Group failures by root cause: one entry per distinct exception, with a count.",
    )
    history = subparsers.add_parser(
        "history",
        parents=[where],
        help="Show a test's outcome in every run still on disk -- is this failure new, or old?",
    )
    history.add_argument("pattern", help="A full nodeid, or any substring that matches one.")
    history.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Look at the newest N runs only (default: every run still on disk).",
    )

    compare = subparsers.add_parser(
        "compare",
        parents=[where],
        help="Diff two runs: which tests started failing, which started passing.",
    )
    compare.add_argument(
        "runs",
        nargs="*",
        type=int,
        metavar="OLD NEW",
        help="Two run numbers (default: the two newest runs on disk).",
    )

    subparsers.add_parser("help", help="Show this message.")
    return parser


def run(argv: Sequence[str]) -> int:
    """Entry point for the query subcommands. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    if args.command == "help":
        parser.print_help()
        return 0
    try:
        return _dispatch(args)
    except QueryError as error:
        print(f"pytest-agent: {error}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    # history and compare read every run on disk rather than one, so they
    # resolve their own inputs instead of taking the single run dir below.
    if args.command == "history":
        return _cmd_history(args)
    if args.command == "compare":
        return _cmd_compare(args)
    run_dir = _resolve_run_dir(args.dir, args.run)
    records = _load_records(run_dir)
    if args.command == "show":
        return _cmd_show(args, run_dir, records)
    if args.command == "last-failures":
        return _cmd_last_failures(args, run_dir, records)
    return _cmd_digest(run_dir, records)


def _resolve_agent_dir(explicit: str | None) -> Path:
    """Locate the agent directory to read.

    Searching upward from the cwd (rather than only looking in it) is what
    makes these commands usable from a subdirectory of the project, which is
    where an agent inspecting one package's tests usually is.
    """
    name = explicit if explicit is not None else os.environ.get("PYTEST_AGENT_DIR", ".pytest-agent")
    candidate = Path(name)
    if candidate.is_absolute() or explicit is not None:
        directory = candidate if candidate.is_absolute() else Path.cwd() / candidate
        if not directory.is_dir():
            raise QueryError(f"no such agent directory: {display_path(directory)}")
        return directory

    start = Path.cwd()
    for parent in (start, *start.parents):
        found = parent / name
        if found.is_dir():
            return found
    # Spelled out in full here, not shortened to "." by display_path: when
    # the answer is "there is nothing to read", where you were looking is
    # the whole content of the message.
    raise QueryError(
        f"no {name}/ found in {start} or any parent directory -- "
        "run `pytest --agent` first, or point at one with --dir",
    )


def _resolve_run_dir(explicit_dir: str | None, run: int | None) -> Path:
    """The run directory to read: run N if asked for, else the newest usable one.

    "Newest" is the highest-numbered run whose index.jsonl exists, rather
    than history.jsonl's last line: a run killed by ^C (or one that segfaulted)
    never gets a history entry, and that is exactly the run someone is most
    likely to be asking about.
    """
    agent_dir = _resolve_agent_dir(explicit_dir)
    if run is not None:
        return _run_dir_for(agent_dir, run)

    usable = _usable_run_numbers(agent_dir)
    if not usable:
        raise QueryError(f"no completed runs under {display_path(agent_dir)} -- run `pytest --agent` first")
    return agent_dir / f"runs-{usable[-1]:04d}"


def _run_dir_for(agent_dir: Path, number: int) -> Path:
    candidate = agent_dir / f"runs-{number:04d}"
    if not candidate.is_dir():
        available = sorted(existing_run_numbers(agent_dir))
        have = ", ".join(str(present) for present in available) if available else "none"
        raise QueryError(f"run {number} not found under {display_path(agent_dir)} (runs present: {have})")
    return candidate


def _usable_run_numbers(agent_dir: Path) -> list[int]:
    """Runs that recorded something, oldest first.

    A run that claimed its directory and then died before writing a single
    record has nothing to say; leaving it in would make it the "newest run"
    for every query and hide the last run that actually ran tests.
    """
    return sorted(
        number
        for number in existing_run_numbers(agent_dir)
        if (agent_dir / f"runs-{number:04d}" / "index.jsonl").is_file()
    )


def _load_records(run_dir: Path) -> list[dict[str, Any]]:
    index_path = run_dir / "index.jsonl"
    if not index_path.is_file():
        raise QueryError(f"{display_path(index_path)} does not exist -- that run recorded nothing")
    _warn_if_still_running(run_dir)
    lines = index_path.read_text(encoding="utf-8").splitlines()
    populated = [number for number, line in enumerate(lines, start=1) if line.strip()]
    last_populated = populated[-1] if populated else 0
    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # index.jsonl is appended to as each test finishes, so a run
            # killed mid-write can leave one truncated *final* line. Every
            # complete line before it is still perfectly good data. Anywhere
            # else it means real corruption, and silently dropping the line
            # would under-report failures in a count an agent is trusting.
            if number != last_populated:
                raise QueryError(f"{display_path(index_path)} is corrupt: line {number} is not valid JSON") from None
    return records


def failing_nodeids(explicit_dir: str | None, run: int | None) -> tuple[list[str], Path]:
    """The failing nodeids of one recorded run, and the run they came from.

    Public because `pytest-agent rerun` (in cli.py) turns these straight into
    pytest arguments. Reading them from the archive rather than from pytest's
    own `--lf` cache is the point: the cache only ever remembers the last run
    in a rootdir, so re-running a subset overwrites it, while runs-NNNN keeps
    every run until it is pruned.
    """
    run_dir = _resolve_run_dir(explicit_dir, run)
    records = _load_records(run_dir)
    return [str(record["nodeid"]) for record in records if record["outcome"] in FAILING_OUTCOMES], run_dir


def _warn_if_still_running(run_dir: Path) -> None:
    """Say so when the run being read has not finished yet.

    index.jsonl is appended to as each test finishes, so a run in progress
    reads as a complete run that happened to have fewer tests -- and "0
    failures" from a run that is 3% done is the most confidently wrong answer
    this tool could give. The lock that keeps a live run from being pruned
    makes this detectable, so it gets said.

    Emitted here, where every command loads its records, rather than in each
    command: a new subcommand cannot forget it. On stderr because it is a
    caveat about the answer rather than part of it -- these outputs get piped.

    The wording names the run rather than the output because history and
    compare read many runs at once: "what follows is partial" would be false
    for the other six rows of a history table, and an agent that believed it
    would throw away good data to be safe.
    """
    if not run_is_locked(run_dir, time.time()):
        return
    print(
        f"pytest-agent: {run_dir.name} is still running -- its records are incomplete",
        file=sys.stderr,
    )


def _run_label(run_dir: Path) -> str:
    return f"{run_dir.name} ({display_path(run_dir)})"


def _print_detail(run_dir: Path, record: dict[str, Any]) -> None:
    log_path = run_dir / record["log_file"]
    print(f"=== {record['nodeid']} [{record['outcome']}] {display_path(log_path)}")
    if not log_path.is_file():
        print(f"(no log file at {display_path(log_path)} -- it may have been pruned)")
        return
    print(log_path.read_text(encoding="utf-8").rstrip("\n"))


def _cmd_show(args: argparse.Namespace, run_dir: Path, records: list[dict[str, Any]]) -> int:
    pattern = str(args.pattern)
    matches = [record for record in records if record["nodeid"] == pattern]
    if not matches:
        matches = [record for record in records if pattern in record["nodeid"]]
    if not matches:
        raise QueryError(
            f"no test matching {pattern!r} in {_run_label(run_dir)} ({len(records)} tests recorded) -- "
            "`pytest-agent last-failures` lists the failing ones",
        )
    if len(matches) > 1 and not args.all:
        print(f"{len(matches)} tests in {_run_label(run_dir)} match {pattern!r}:")
        for record in matches:
            print(f"  [{record['outcome']}] {record['nodeid']}")
        print("narrow the pattern, or pass --all to print all of them")
        return 1
    for index, record in enumerate(matches):
        if index:
            print()
        _print_detail(run_dir, record)
    return 0


def _cmd_last_failures(args: argparse.Namespace, run_dir: Path, records: list[dict[str, Any]]) -> int:
    failures = [record for record in records if record["outcome"] in FAILING_OUTCOMES]
    print(f"{_run_label(run_dir)}: {len(failures)} failed/errored of {len(records)} recorded")
    for record in failures:
        if args.detail:
            print()
            _print_detail(run_dir, record)
            continue
        # Same one-line-per-failure shape as the end-of-run summary: the log
        # path spells out the nodeid, so printing both would be redundant.
        line = display_path(run_dir / record["log_file"])
        if not nodeid_is_evident_from(str(record["log_file"]), str(record["nodeid"])):
            line = f"{line}  ({record['nodeid']})"
        print(line)
        crash = _crash_of(record)
        if crash is not None:
            print(f"    {crash.message}")
    return 0


def _load_run(agent_dir: Path, number: int) -> RunResult:
    run_dir = _run_dir_for(agent_dir, number)
    # Last record wins for a nodeid recorded twice (a rerun plugin, or a
    # parametrization collected twice): the later outcome is the run's answer.
    by_nodeid = {str(record["nodeid"]): record for record in _load_records(run_dir)}
    return RunResult(number=number, run_dir=run_dir, by_nodeid=by_nodeid)


def _scanned_line(numbers: list[int]) -> str:
    """What was actually read -- said out loud, because it is not the whole history.

    `--agent-keep-runs` deletes old runs-* directories while history.jsonl keeps
    its entries forever, so "failed 2 of 3" means 3 runs *still on disk*. An
    agent reaching for this command is usually about to claim a failure is
    pre-existing, and that claim is only as good as its denominator.
    """
    span = f"runs-{numbers[0]:04d}" if len(numbers) == 1 else f"runs-{numbers[0]:04d}..runs-{numbers[-1]:04d}"
    plural = "run" if len(numbers) == 1 else "runs"
    return f"{len(numbers)} {plural} on disk ({span}); older runs are pruned, so this is not the full history"


def _cmd_history(args: argparse.Namespace) -> int:
    agent_dir = _resolve_agent_dir(args.dir)
    numbers = _usable_run_numbers(agent_dir)
    if not numbers:
        raise QueryError(f"no completed runs under {display_path(agent_dir)} -- run `pytest --agent` first")
    limit = int(args.limit)
    if limit > 0:
        numbers = numbers[-limit:]
    runs = [_load_run(agent_dir, number) for number in numbers]

    pattern = str(args.pattern)
    nodeids = _matching_nodeids(runs, pattern)
    if not nodeids:
        raise QueryError(f"no test matching {pattern!r} in {_scanned_line(numbers)}")

    print(_scanned_line(numbers))
    for nodeid in nodeids[:MAX_LISTED_NODEIDS]:
        print()
        _print_history(nodeid, runs)
    if len(nodeids) > MAX_LISTED_NODEIDS:
        print(f"\n+{len(nodeids) - MAX_LISTED_NODEIDS} more tests match {pattern!r} -- narrow the pattern")
    return 0


def _matching_nodeids(runs: list[RunResult], pattern: str) -> list[str]:
    """Every nodeid matching *pattern* in any run, in the newest run's order.

    Matched across all runs, not just the newest: a test that was deleted or
    renamed is precisely the one whose history somebody is asking about.
    """
    seen: dict[str, None] = {}
    for run in reversed(runs):
        for nodeid in run.by_nodeid:
            if nodeid == pattern:
                return [nodeid]
            if pattern in nodeid:
                seen.setdefault(nodeid, None)
    return list(seen)


def _print_history(nodeid: str, runs: list[RunResult]) -> None:
    ran = [run for run in runs if run.outcome_of(nodeid) is not None]
    failed = [run for run in ran if run.failed(nodeid)]
    scope = f"{len(ran)} runs that ran it" if len(ran) != len(runs) else f"{len(runs)} runs"
    print(f"{nodeid} -- failed in {len(failed)} of the {scope}")
    for run in reversed(runs):
        record = run.by_nodeid.get(nodeid)
        if record is None:
            print(f"  {run.run_dir.name}  (not in this run)")
            continue
        duration = record.get("duration_s")
        elapsed = f"{duration:>7.2f}s" if isinstance(duration, (int, float)) else " " * 8
        print(f"  {run.run_dir.name}  {record['outcome']!s:<9}{elapsed}")
        crash = _crash_of(record)
        if crash is not None:
            print(f"      {_one_line(crash.message)}")


def _one_line(message: str) -> str:
    """A crash message shrunk to fit under a list entry.

    These lists exist to be scanned -- if one test's message is a 40-line
    assertion diff, the shape of the run stops being visible. `show` prints
    the whole thing.
    """
    first = message.strip().splitlines()[0] if message.strip() else message
    return first if len(first) <= MAX_MESSAGE_CHARS else f"{first[:MAX_MESSAGE_CHARS]}..."


def _cmd_compare(args: argparse.Namespace) -> int:
    agent_dir = _resolve_agent_dir(args.dir)
    old, new = _runs_to_compare(agent_dir, [int(number) for number in args.runs])

    shared = [nodeid for nodeid in new.by_nodeid if nodeid in old.by_nodeid]
    newly_failing = [nodeid for nodeid in shared if new.failed(nodeid) and not old.failed(nodeid)]
    newly_passing = [nodeid for nodeid in shared if old.failed(nodeid) and not new.failed(nodeid)]
    still_failing = [nodeid for nodeid in shared if old.failed(nodeid) and new.failed(nodeid)]

    print(
        f"{old.run_dir.name} -> {new.run_dir.name}: "
        f"{len(newly_failing)} newly failing, {len(newly_passing)} newly passing, "
        f"{len(still_failing)} still failing ({len(shared)} tests in both runs)",
    )
    _print_changed("newly failing", newly_failing, new)
    _print_changed("newly passing", newly_passing, old)
    _print_changed("still failing", still_failing, new)

    if not (newly_failing or newly_passing or still_failing):
        # Printed rather than left as silence: nothing at all reads as "the
        # command didn't work", which costs a turn to rule out.
        print("no outcome changed between these runs")
    # Counted, not listed, and last: the usual cause is one of the two runs
    # being a filtered `-k` re-run, where "these 900 tests are missing" is
    # noise above the answer.
    for label, other, run in ((new.run_dir.name, old, new), (old.run_dir.name, new, old)):
        only = [nodeid for nodeid in run.by_nodeid if nodeid not in other.by_nodeid]
        if only:
            print(f"only in {label}: {len(only)} tests (`pytest-agent last-failures --run {run.number}`)")
    return 0


def _runs_to_compare(agent_dir: Path, requested: list[int]) -> tuple[RunResult, RunResult]:
    if len(requested) == 1:
        raise QueryError("compare takes two run numbers, or none at all (the two newest runs on disk)")
    if len(requested) > 2:
        raise QueryError(f"compare takes at most two run numbers, got {len(requested)}")
    if requested:
        return _load_run(agent_dir, requested[0]), _load_run(agent_dir, requested[1])
    numbers = _usable_run_numbers(agent_dir)
    if len(numbers) < 2:
        raise QueryError(
            f"only {len(numbers)} run(s) under {display_path(agent_dir)} -- "
            "compare needs two (run `pytest --agent` again)",
        )
    return _load_run(agent_dir, numbers[-2]), _load_run(agent_dir, numbers[-1])


def _print_changed(title: str, nodeids: list[str], run: RunResult) -> None:
    if not nodeids:
        return
    print(f"{title}:")
    for nodeid in nodeids[:MAX_LISTED_NODEIDS]:
        print(f"  {nodeid}")
        crash = _crash_of(run.by_nodeid[nodeid])
        if crash is not None:
            print(f"    {_one_line(crash.message)}")
    if len(nodeids) > MAX_LISTED_NODEIDS:
        print(f"  +{len(nodeids) - MAX_LISTED_NODEIDS} more")


def _crash_of(record: dict[str, Any]) -> Crash | None:
    """The crash field of one record, or None if it has none.

    Every field is re-validated rather than trusted: these records come off
    disk, and runs written by an older pytest-agent (up to --agent-keep-runs
    of them can still be sitting there after an upgrade) have no crash field
    at all. Missing structured data degrades to a thinner answer, never a
    traceback out of the query itself.
    """
    raw = record.get("crash")
    if not isinstance(raw, dict):
        return None
    crash = cast("dict[str, object]", raw)
    message = crash.get("message")
    if not isinstance(message, str) or not message:
        return None
    path = crash.get("path")
    lineno = crash.get("lineno")
    location = None
    if isinstance(path, str) and path:
        location = f"{path}:{lineno}" if isinstance(lineno, int) else path
    return Crash(message=message, location=location)


def _frames_of(record: dict[str, Any]) -> list[Frame]:
    raw = record.get("frames")
    if not isinstance(raw, list):
        return []
    frames: list[Frame] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, object]", item)
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        lineno = entry.get("lineno")
        frames.append(
            Frame(
                path=path,
                lineno=lineno if isinstance(lineno, int) else None,
                first_party=bool(entry.get("first_party")),
            ),
        )
    return frames


def _group_failures(records: list[dict[str, Any]]) -> list[FailureGroup]:
    """Collapse failing records into one entry per distinct root cause.

    Keyed on the *normalized* message so that N tests failing on the same
    missing file group together even when the message embeds a per-test temp
    path; each group keeps one raw message so the reader still sees a real
    path rather than the placeholder-riddled key.
    """
    groups: dict[str, FailureGroup] = {}
    for record in records:
        crash = _crash_of(record)
        key = normalize_message(crash.message) if crash is not None else _NO_CRASH_GROUP
        group = groups.get(key)
        if group is None:
            frames = _frames_of(record)
            first_party = [frame for frame in frames if frame.first_party]
            group = FailureGroup(
                message=crash.message if crash is not None else "",
                location=crash.location if crash is not None else None,
                frames=first_party or frames,
                frames_are_first_party=bool(first_party),
            )
            groups[key] = group
        group.nodeids.append(str(record["nodeid"]))
    return sorted(groups.values(), key=lambda group: len(group.nodeids), reverse=True)


def _frame_location(frame: Frame) -> str:
    return f"{frame.path}:{frame.lineno if frame.lineno is not None else '?'}"


def _print_group(index: int, group: FailureGroup) -> None:
    headline = group.message or "(no crash info recorded -- an older pytest-agent wrote this run)"
    print(f"[{index}] {len(group.nodeids)}x  {headline}")
    if group.frames:
        label = "first-party frames" if group.frames_are_first_party else "frames (none in first-party code)"
        print(f"     {label}, outermost first:")
        for frame in group.frames:
            print(f"       {_frame_location(frame)}")
        # Where it actually raised, but only when that isn't already the
        # last line above: for a failure inside a library the crash site is
        # in stdlib/site-packages and the frames stop at your own call into
        # it, while for a plain assert the two are the same line and
        # printing both would just be noise.
        if group.location and group.location != _frame_location(group.frames[-1]):
            print(f"       -> raised in {group.location}")
    elif group.location:
        print(f"     raised at {group.location}")
    for nodeid in group.nodeids[:MAX_LISTED_NODEIDS]:
        print(f"     - {nodeid}")
    hidden = len(group.nodeids) - MAX_LISTED_NODEIDS
    if hidden > 0:
        print(f"     ... and {hidden} more (pytest-agent last-failures)")


def _cmd_digest(run_dir: Path, records: list[dict[str, Any]]) -> int:
    failures = [record for record in records if record["outcome"] in FAILING_OUTCOMES]
    if not failures:
        print(f"{_run_label(run_dir)}: no failures out of {len(records)} tests recorded")
        return 0
    groups = _group_failures(failures)
    plural = "" if len(groups) == 1 else "s"
    print(
        f"{_run_label(run_dir)}: {len(failures)} failed/errored of {len(records)} recorded, "
        f"{len(groups)} distinct root cause{plural}",
    )
    for index, group in enumerate(groups, start=1):
        print()
        _print_group(index, group)
    print()
    print("full detail for any one of these: pytest-agent show '<nodeid or substring>'")
    return 0
