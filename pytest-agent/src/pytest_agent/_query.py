"""`pytest-agent show|last-failures|digest` -- read-only queries over what a
previous agent-mode run wrote to disk.

The point of these is that reading one failure should not require the agent
to *construct* anything. Before them, getting at a single test's detail meant
knowing the run number, mirroring the test file's path into the run
directory, and shell-quoting the brackets in a parametrized id -- three
independent chances to get it wrong, each costing a whole turn.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pytest_agent._capture import nodeid_is_evident_from
from pytest_agent._crash import normalize_message
from pytest_agent._history import existing_run_numbers
from pytest_agent._paths import display_path

if TYPE_CHECKING:
    from collections.abc import Sequence

# argv[0] values that mean "query", not "run pytest". Everything else the
# console script sees is forwarded to pytest untouched.
SUBCOMMANDS = ("show", "last-failures", "digest", "help")

FAILING_OUTCOMES = frozenset({"failed", "error", "collect_error"})

# Failures per digest group listed in full before collapsing into a count.
# The whole point of the digest is to answer "are these all the same bug?" --
# an exhaustive nodeid list per group works against that.
MAX_LISTED_NODEIDS = 10

_NO_CRASH_GROUP = "\x00no-crash"

_EPILOG = """\
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
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--run",
        type=int,
        default=None,
        metavar="N",
        help="Query run N instead of the most recent one.",
    )
    common.add_argument(
        "--dir",
        default=None,
        metavar="PATH",
        help="Agent directory to read (default: the nearest .pytest-agent at or above the cwd).",
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
        run_dir = _resolve_run_dir(args.dir, args.run)
        records = _load_records(run_dir)
        if args.command == "show":
            return _cmd_show(args, run_dir, records)
        if args.command == "last-failures":
            return _cmd_last_failures(args, run_dir, records)
        return _cmd_digest(run_dir, records)
    except QueryError as error:
        print(f"pytest-agent: {error}", file=sys.stderr)
        return 1


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
        candidate = agent_dir / f"runs-{run:04d}"
        if not candidate.is_dir():
            available = sorted(existing_run_numbers(agent_dir))
            have = ", ".join(str(number) for number in available) if available else "none"
            raise QueryError(f"run {run} not found under {display_path(agent_dir)} (runs present: {have})")
        return candidate

    usable = sorted(
        number
        for number in existing_run_numbers(agent_dir)
        if (agent_dir / f"runs-{number:04d}" / "index.jsonl").is_file()
    )
    if not usable:
        raise QueryError(f"no completed runs under {display_path(agent_dir)} -- run `pytest --agent` first")
    return agent_dir / f"runs-{usable[-1]:04d}"


def _load_records(run_dir: Path) -> list[dict[str, Any]]:
    index_path = run_dir / "index.jsonl"
    if not index_path.is_file():
        raise QueryError(f"{display_path(index_path)} does not exist -- that run recorded nothing")
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
