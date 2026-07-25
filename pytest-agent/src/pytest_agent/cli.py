from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

import pytest

from pytest_agent._entry_points import PLUGIN_MODULE, plugin_registered_via_entry_points
from pytest_agent._paths import display_path
from pytest_agent._query import SUBCOMMANDS, QueryError, failing_nodeids
from pytest_agent._query import run as run_query

# Not in SUBCOMMANDS: those are the read-only queries, and this one is the
# opposite -- it reads the archive only to decide what pytest should run next.
RERUN_COMMAND = "rerun"


def main() -> None:
    """Console-script entry point.

    `pytest-agent <show|last-failures|digest|history|compare|help> ...` queries
    what a previous run wrote to disk, `pytest-agent rerun` re-runs a recorded
    run's failures, and anything else is `pytest --agent [args...]`.

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
    if argv and argv[0] == RERUN_COMMAND:
        raise SystemExit(_rerun(argv[1:]))
    near_miss = _misspelled_subcommand(argv)
    if near_miss is not None:
        sys.stderr.write(
            f"pytest-agent: no such subcommand or path: {argv[0]!r} -- did you mean '{near_miss}'?\n",
        )
        raise SystemExit(2)
    raise SystemExit(_run_pytest(argv))


def _run_pytest(args: list[str]) -> int:
    extra_plugins: list[str] = [] if plugin_registered_via_entry_points() else [PLUGIN_MODULE]
    return int(pytest.main(["--agent", *args], plugins=extra_plugins))


def _rerun(argv: list[str]) -> int:
    """`pytest-agent rerun` -- run exactly the tests that failed in a recorded run.

    pytest's own `--lf` answers this from a cache holding only the *last* run
    in a rootdir, so the first re-run of a subset overwrites the list an agent
    is working through, and a `-k`-filtered run overwrites it with a fragment.
    Reading runs-NNNN instead means `--run N` can re-run a failure set from any
    run still on disk, and the ids come back already exact -- no quoting a
    parametrized `[in_process-local]` through a shell that treats brackets as
    globs.
    """
    parser = argparse.ArgumentParser(
        prog="pytest-agent rerun",
        description="Re-run the tests that failed in a recorded run. Unrecognized arguments go to pytest.",
        # Off so that a project's own `--run-slow`-style flag is passed
        # through to pytest rather than being abbreviation-matched to --run.
        allow_abbrev=False,
    )
    parser.add_argument("--run", type=int, default=None, metavar="N", help="Re-run run N's failures.")
    parser.add_argument("--dir", default=None, metavar="PATH", help="Agent directory to read.")
    args, forwarded = parser.parse_known_args(argv)

    try:
        nodeids, run_dir = failing_nodeids(args.dir, args.run)
    except QueryError as error:
        sys.stderr.write(f"pytest-agent: {error}\n")
        return 1

    if not nodeids:
        # Emphatically *not* falling through to pytest: with no nodeids the
        # forwarded arguments would select the entire suite, turning "re-run
        # my failures" into a full run of a suite that had none.
        print(f"nothing to re-run: {display_path(run_dir)} recorded no failures")
        return 0

    # flush: pytest writes straight to the terminal, so without this the line
    # saying what is about to run turns up *after* the run it announced
    # whenever stdout is a pipe rather than a tty.
    print(f"re-running {len(nodeids)} failed from {display_path(run_dir)}", flush=True)
    return _run_pytest([*nodeids, *forwarded])


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
    matches = difflib.get_close_matches(argv[0], (*SUBCOMMANDS, RERUN_COMMAND), n=1)
    return matches[0] if matches else None
