from __future__ import annotations

import difflib
import sys
from pathlib import Path

import pytest

from pytest_agent._entry_points import PLUGIN_MODULE, plugin_registered_via_entry_points
from pytest_agent._query import SUBCOMMANDS
from pytest_agent._query import run as run_query


def main() -> None:
    """Console-script entry point.

    `pytest-agent <show|last-failures|digest|help> ...` queries what a
    previous run wrote to disk; anything else is `pytest --agent [args...]`.

    Dispatch is on an exact match of the first argument. pytest's own
    arguments are paths, nodeids, or `-`-prefixed flags, so the only
    collision is a top-level path literally named e.g. `show`, which is still
    reachable as `pytest-agent ./show`. Queries deliberately never go through
    pytest.main(): their output is exactly what an agent has good reason to
    pipe into grep, and routing it through pytest would put it behind the
    piped-stdout guard for nothing.

    The pytest path runs in-process via pytest's own public `pytest.main()`
    API rather than spawning a subprocess. When this package is properly
    installed (a real `pip install`/nix package), its own pytest11 entry point
    already makes pytest load the plugin, and passing it again via `plugins=`
    crashes: pluggy registers it once under the entry point's declared name
    ("agent") and refuses a second registration of the same module object
    under the different name pytest.main()'s `plugins=` list would use. So the
    plugin is only passed explicitly as a fallback, for when pytest-agent is
    merely on PYTHONPATH with no installed distribution metadata at all (as in
    its own dev/test environment) and entry-point discovery has nothing to
    find.
    """
    argv = sys.argv[1:]
    if argv and argv[0] in SUBCOMMANDS:
        raise SystemExit(run_query(argv))
    near_miss = _misspelled_subcommand(argv)
    if near_miss is not None:
        sys.stderr.write(
            f"pytest-agent: no such subcommand or path: {argv[0]!r} -- did you mean '{near_miss}'?\n",
        )
        raise SystemExit(2)
    extra_plugins: list[str] = [] if plugin_registered_via_entry_points() else [PLUGIN_MODULE]
    raise SystemExit(pytest.main(["--agent", *argv], plugins=extra_plugins))


def _misspelled_subcommand(argv: list[str]) -> str | None:
    """The subcommand *argv* was probably reaching for, if it missed by a typo.

    Without this, `pytest-agent lastfailures` is forwarded to pytest and comes
    back as "file or directory not found: lastfailures", which reads like a
    problem with the test path rather than with the subcommand name. Only a
    first argument that is not a flag, does not exist as a path, and is a near
    match for a real subcommand qualifies -- a nodeid or an ordinary test path
    is nowhere near any of them.
    """
    if not argv or argv[0].startswith("-") or Path(argv[0]).exists():
        return None
    matches = difflib.get_close_matches(argv[0], SUBCOMMANDS, n=1)
    return matches[0] if matches else None
