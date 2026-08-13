"""The pynixd suite must not read the working directory.

`TEST_NIX` was `Path("tests/nix")`, and `nix build --file tests/nix` resolved
it against whatever directory pytest started in. The suite therefore ran from
`pynixd/` alone. From the root of this repository the same command reported
`path '/home/lillecarl/Code/nanopynix/tests/nix' does not exist`, and 30 tests
failed with a message that named neither the cause nor the fix.

This matters beyond a convenience. A `nix build --file . checks.pynixd` gate
runs the suite from a copy of the source in the store, under a build
directory that no test chooses. A suite that reads the working directory
cannot become that gate.

The check is textual on purpose. Importing the fixtures of pynixd needs the
whole environment of pynixd, and this rule is about what the source says.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.support.suite_roots import REPO_ROOT

PYNIXD_TESTS = REPO_ROOT / "pynixd" / "tests"
CONSTANTS = PYNIXD_TESTS / "_conftest" / "constants.py"
# The one assignment that every consumer of the Nix fixtures reads.
TEST_NIX_ASSIGNMENT = re.compile(r"^TEST_NIX = (.+)$", re.MULTILINE)


def _test_modules() -> list[Path]:
    return [path for path in sorted(PYNIXD_TESTS.rglob("*.py")) if ".pytest-agent" not in path.parts]


def test_the_nix_fixture_directory_is_anchored_on_a_file() -> None:
    """A relative `TEST_NIX` is the defect itself, and it reads as correct."""
    found = TEST_NIX_ASSIGNMENT.search(CONSTANTS.read_text())
    assert found, f"{CONSTANTS} no longer assigns TEST_NIX. Move this check to wherever the fixtures now live."
    value = found.group(1)
    assert "__file__" in value, (
        f"TEST_NIX is {value!r}, which the working directory resolves. "
        "Anchor it on `Path(__file__)`, so that the suite runs from any directory."
    )


def test_no_test_module_writes_the_fixture_path_itself() -> None:
    """Three modules held their own copy, and each one was relative.

    A copy that agrees today is a copy that stops agreeing. Import `TEST_NIX`
    from `tests.conftest` instead.
    """
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _test_modules()
        if path != CONSTANTS and re.search(r"""["']tests/nix["']""", path.read_text())
    ]
    assert not offenders, (
        f"these modules spell the fixture directory themselves: {offenders}. "
        "Import TEST_NIX from tests.conftest, which anchors it on a file."
    )
