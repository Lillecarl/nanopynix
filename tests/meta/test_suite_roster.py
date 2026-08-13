"""The roster of suites must match the repository, in both directions.

``tests/support/suite_roots.py`` names every directory that a repository-wide
scanner reads. ``check_roster()`` answers one half: an entry that names nothing
is a scanner pointed at a tree that is not there. This module answers the other
half, which is the half that actually went wrong.

**Three self-checks were widened by hand, once per move of issue #130, and each
one passed in between.** A scanner finds no offender in a directory it never
opened, so the failure mode is silence. ``tests/meta/test_agent_note_imports``
kept reading ``tests/`` after the helpers suite left it;
``test_no_collector_rule`` counted test modules under a ``tests/`` that no
longer held them and still asserted "more than 50".

The check below is mechanical, which is the whole point. Every ``pytest.ini``
declares the suite it runs in ``testpaths``. A suite that exists is therefore a
suite that some ini names, and the roster has to name it too.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tests.support.suite_roots import REPO_ROOT, SUITE_ROOTS, check_roster

# Subprojects whose suites this repository's scanners deliberately do not read.
# Each has its own rootdir, its own gate in `nix/checks.nix`, and its own
# conventions, so a scanner that enforces a nanopynix rule over them would
# report a finding that is not a defect.
#
# `grpclib-transports` is the case that names the reason: `ruff-strict.toml`
# gives it its own per-file ignores, because the ban on the raw `asyncio`
# primitives is right for nanopynix and wrong for a library whose subject is
# `asyncio.Protocol` callbacks.
EXEMPT_INIS = {
    "grpclib-transports/pytest.ini",
}


def _declared_suites() -> dict[str, list[Path]]:
    """Every `testpaths` of every `pytest.ini`, resolved against its own file.

    Keyed by the repository-relative path of the ini, so a failure names the
    file to edit.
    """
    found: dict[str, list[Path]] = {}
    for ini in sorted(REPO_ROOT.rglob("pytest.ini")):
        if ".pytest-agent" in ini.parts or "result" in ini.parts:
            continue
        relative = str(ini.relative_to(REPO_ROOT))
        if relative in EXEMPT_INIS:
            continue
        parser = configparser.ConfigParser()
        parser.read(ini)
        raw = parser.get("pytest", "testpaths", fallback="")
        found[relative] = [(ini.parent / entry).resolve() for entry in raw.split()]
    return found


def test_the_exemptions_still_name_a_real_file() -> None:
    """An exemption for an ini that moved is an exemption that hides a suite."""
    missing = sorted(name for name in EXEMPT_INIS if not (REPO_ROOT / name).is_file())
    assert not missing, f"EXEMPT_INIS names {missing}, and no such file exists. Remove the entry or correct it."


def test_every_ini_declares_a_suite() -> None:
    """A `pytest.ini` with no `testpaths` runs whatever the caller points at.

    That is the shape this gate cannot check, so it is refused rather than
    skipped.
    """
    empty = sorted(name for name, paths in _declared_suites().items() if not paths)
    assert not empty, (
        f"these pytest.ini files declare no testpaths: {empty}. "
        "Name the suite there, so that tests/support/suite_roots.py can be checked against it."
    )


def test_the_roster_names_every_declared_suite() -> None:
    """The decay mode that three self-checks hit: a suite the roster omits.

    A scanner reads `SUITE_ROOTS`. A suite that no entry covers is a suite that
    every repository-wide rule silently stops applying to.
    """
    check_roster()
    known = {root.resolve() for root in SUITE_ROOTS}
    unlisted = sorted(
        f"{path.relative_to(REPO_ROOT)} (from {ini})"
        for ini, paths in _declared_suites().items()
        for path in paths
        if path not in known
    )
    assert not unlisted, (
        f"these suites are declared in a pytest.ini and missing from SUITE_ROOTS: {unlisted}. "
        "Add each one to tests/support/suite_roots.py. Until you do, every scanner that reads "
        "that roster passes without looking at them."
    )


def test_the_roster_lists_no_suite_twice() -> None:
    """A duplicate makes every scanner read the same tree twice, and a count wrong."""
    resolved = [root.resolve() for root in SUITE_ROOTS]
    duplicates = sorted({str(root) for root in resolved if resolved.count(root) > 1})
    assert not duplicates, f"SUITE_ROOTS lists {duplicates} more than once"


@pytest.mark.parametrize("root", SUITE_ROOTS, ids=lambda root: root.name)
def test_each_suite_root_holds_a_test_module(root: Path) -> None:
    """A roster entry that exists and holds nothing is the same silence again."""
    modules = [path for path in root.rglob("test_*.py") if ".pytest-agent" not in path.parts]
    assert modules, f"{root} is on the roster and holds no test module"
