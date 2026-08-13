"""No test module may import ``pytest_agent`` directly.

The packaged test runner (``nanopynix/tests.nix``) leaves pytest-agent out on
purpose: it auto-activates on import, and that runner is what CI executes. So
``from pytest_agent import note`` at module level fails at *collection* there,
on every Nix version and both backends, while every dev shell passes.

That is not a hypothetical. It happened twice. The first time cost the
``agent_notes`` fixture fallback in ``tests/conftest.py``, and the second left
CI red for a day across four modules, reporting a collection error rather than
anything about the code under test. Both times the local suite was green.

``test_support.notes`` is the supported import. This test is the gate that
keeps callers pointed at it, because pytest is the only check that runs on
every version and both backends -- the same reasoning as
``tests/meta/test_suppression_grammar.py``.

``test-support/src/test_support/notes.py`` and ``tests/conftest.py`` are
exempt: they are the two places that must ask whether the plugin is there.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"
TEST_SUPPORT_ROOT = REPO_ROOT / "test-support"
NOTES = TEST_SUPPORT_ROOT / "src" / "test_support" / "notes.py"

# Both trees, because issue #130 moved the helper out of `tests/` and a scanner
# that reads one tree would stop seeing the other. `test-support/` holds a
# suite of its own as well as the helper.
SCAN_ROOTS = (TESTS_ROOT, TEST_SUPPORT_ROOT)

# The two modules whose job is to detect the plugin. Everything else goes
# through test_support.notes.
EXEMPT = {
    NOTES,
    TESTS_ROOT / "conftest.py",
}

# `import pytest_agent`, `from pytest_agent import ...`, and
# `importlib.import_module("pytest_agent")`. A string mention in a docstring
# or a comment is not a match, because the name must follow an import keyword.
_IMPORT = re.compile(r"^\s*(?:from|import)\s+pytest_agent\b", re.MULTILINE)


def _scanned() -> list[Path]:
    found = [path for root in SCAN_ROOTS for path in root.rglob("*.py")]
    return sorted(path for path in found if ".pytest-agent" not in path.parts)


def _offenders() -> list[str]:
    found: list[str] = []
    for path in _scanned():
        if path in EXEMPT:
            continue
        for match in _IMPORT.finditer(path.read_text()):
            line = path.read_text().count("\n", 0, match.start()) + 1
            found.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    return found


def test_the_scanner_can_see_the_test_tree() -> None:
    """A scanner that matches nothing would pass forever and enforce nothing."""
    assert NOTES.exists()
    assert NOTES in set(_scanned())
    assert len(_scanned()) > 100


def test_the_scanner_matches_the_shape_it_is_looking_for() -> None:
    assert _IMPORT.search("from pytest_agent import note")
    assert _IMPORT.search("import pytest_agent")
    assert _IMPORT.search("    from pytest_agent import note  # inside a block")
    # A mention that is not an import must not match.
    assert not _IMPORT.search('"""pytest_agent writes the detail to disk."""')
    assert not _IMPORT.search("from test_support.notes import note")


def test_no_test_module_imports_pytest_agent_directly() -> None:
    offenders = _offenders()
    assert not offenders, (
        "these modules import pytest_agent directly, which fails collection in the "
        "packaged runner that CI executes; import `note` from test_support.notes "
        "instead:\n  " + "\n  ".join(offenders)
    )
