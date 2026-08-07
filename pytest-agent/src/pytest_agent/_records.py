"""Reading what a run wrote: locating the archive, and one record's crash.

Split out of ``_query`` when ``_watch`` arrived and needed the same pieces.
Both modules read the same records, so a shared home was the alternative to
one importing the other -- and ``_query`` imports ``_watch`` to dispatch the
subcommand, which makes the other direction a cycle.

Everything here is total. These values come off disk, they may have been
written by an older pytest-agent, and a run killed mid-write can leave a field
half there. A missing or malformed field gives a thinner answer, never a
traceback out of a command whose whole job is explaining somebody else's
failure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pytest_agent._paths import display_path

# A crash message under a list entry is a label, not the failure itself:
# `show` prints the whole thing.
MAX_MESSAGE_CHARS = 160

# The outcomes that mean a test did not pass. `collect_error` is here because
# a module that would not import is a failure of the run, not an absence from
# it.
FAILING_OUTCOMES = frozenset({"failed", "error", "collect_error"})


class QueryError(Exception):
    """A query that could not be answered -- reported to stderr, exit code 1."""


@dataclass(frozen=True)
class Crash:
    """What failed, as recorded by _crash.crash_from_report."""

    message: str
    location: str | None


def crash_of(record: dict[str, Any]) -> Crash | None:
    """The crash field of one record, or None if it has none.

    Every field is re-validated rather than trusted: these records come off
    disk, and runs written by an older pytest-agent (up to --agent-keep-runs
    of them can still be sitting there after an upgrade) have no crash field
    at all.
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


def one_line(message: str) -> str:
    """A crash message shrunk to fit under a list entry.

    These lists exist to be scanned -- if one test's message is a 40-line
    assertion diff, the shape of the run stops being visible. `show` prints
    the whole thing.
    """
    first = message.strip().splitlines()[0] if message.strip() else message
    return first if len(first) <= MAX_MESSAGE_CHARS else f"{first[:MAX_MESSAGE_CHARS]}..."


def resolve_agent_dir(explicit: str | None) -> Path:
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
