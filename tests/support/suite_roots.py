"""Every directory a repository-wide scanner has to read.

**One roster, because issue #130 makes the answer move.** A self-check that
walks the test tree used to write ``REPO_ROOT / "tests"`` and be finished. Each
suite that moves beside the project it tests leaves that expression correct and
incomplete, and an incomplete scanner reports success: it finds no offender in
a tree it never opened. Two self-checks had already been widened by hand, once
per move, before this module existed.

Import ``SCANNED_ROOTS`` rather than building a tuple of paths. The next move
then edits this file and no other.

Nothing here is a pytest ``rootdir``. These are the directories that hold
Python which the repository's own rules apply to, and a scanner reads them as
text.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories that hold test modules. Each one is the `testpaths` of some
# `pytest.ini`: the repository's own, and one per project since issue #130.
SUITE_ROOTS = (
    REPO_ROOT / "tests",
    REPO_ROOT / "nanopynix" / "tests",
    REPO_ROOT / "test-support" / "tests",
    REPO_ROOT / "nanopynix-helpers" / "tests",
    REPO_ROOT / "pynix" / "tests",
    # The language server suite, which issue #107 moved out of `pynix/tests`
    # with the project it tests.
    REPO_ROOT / "pynix-lsp" / "tests",
    # The shell completions of the installed `pynix`, which issue #213 added.
    # The one suite here that the repository's own `testpaths` does not name:
    # every test in it drives fish, bash and zsh on a pty and completes against
    # a store path. `checks.completions` runs it, and `tests/AGENTS.md` says
    # why no job of the CI matrix can.
    REPO_ROOT / "pynix" / "completions" / "tests",
)

# The installed packages that a suite runs on top of. They hold no test, and
# they hold the fixtures, the markers and the helpers, so a scanner that asks
# "where is this fixture defined" has to read them.
HELPER_ROOTS = (
    REPO_ROOT / "test-support" / "src" / "test_support",
    REPO_ROOT / "nanopynix-testing" / "src" / "nanopynix_testing",
)

SCANNED_ROOTS = SUITE_ROOTS + HELPER_ROOTS


def check_roster() -> None:
    """Fail when an entry names a directory that is not there.

    A scanner that reads a missing root finds nothing and passes, which is the
    failure this roster exists to prevent. Call it from any test that walks
    these paths.
    """
    missing = [str(root) for root in SCANNED_ROOTS if not root.is_dir()]
    if missing:
        raise AssertionError(
            f"tests/support/suite_roots.py names {missing}, and no such directory exists. "
            "A suite moved and the roster did not follow it.",
        )
