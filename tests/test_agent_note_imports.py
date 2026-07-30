"""No test module may import ``pytest_agent`` directly.

The packaged test runner (``nanopynix/tests.nix``) leaves pytest-agent out on
purpose: it auto-activates on import, and that runner is what CI executes. So
``from pytest_agent import note`` at module level fails at *collection* there,
on every Nix version and both backends, while every dev shell passes.

That is not a hypothetical. It happened twice. The first time cost the
``agent_notes`` fixture fallback in ``tests/conftest.py``, and the second left
CI red for a day across four modules, reporting a collection error rather than
anything about the code under test. Both times the local suite was green.

``tests/support/notes.py`` is the supported import. This test is the gate that
keeps callers pointed at it, because pytest is the only check that runs on
every version and both backends -- the same reasoning as
``tests/test_suppression_grammar.py``.

``tests/support/notes.py`` and ``tests/conftest.py`` are exempt: they are the
two places that must ask whether the plugin is there.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"

# The two modules whose job is to detect the plugin. Everything else goes
# through tests/support/notes.py.
EXEMPT = {
    TESTS_ROOT / "support" / "notes.py",
    TESTS_ROOT / "conftest.py",
}

# `import pytest_agent`, `from pytest_agent import ...`, and
# `importlib.import_module("pytest_agent")`. A string mention in a docstring
# or a comment is not a match, because the name must follow an import keyword.
_IMPORT = re.compile(r"^\s*(?:from|import)\s+pytest_agent\b", re.MULTILINE)


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path in EXEMPT or ".pytest-agent" in path.parts:
            continue
        for match in _IMPORT.finditer(path.read_text()):
            line = path.read_text().count("\n", 0, match.start()) + 1
            found.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    return found


def test_the_scanner_can_see_the_test_tree() -> None:
    """A scanner that matches nothing would pass forever and enforce nothing."""
    assert (TESTS_ROOT / "support" / "notes.py").exists()
    assert len(list(TESTS_ROOT.rglob("*.py"))) > 100


def test_the_scanner_matches_the_shape_it_is_looking_for() -> None:
    assert _IMPORT.search("from pytest_agent import note")
    assert _IMPORT.search("import pytest_agent")
    assert _IMPORT.search("    from pytest_agent import note  # inside a block")
    # A mention that is not an import must not match.
    assert not _IMPORT.search('"""pytest_agent writes the detail to disk."""')
    assert not _IMPORT.search("from tests.support.notes import note")


def test_no_test_module_imports_pytest_agent_directly() -> None:
    offenders = _offenders()
    assert not offenders, (
        "these modules import pytest_agent directly, which fails collection in the "
        "packaged runner that CI executes; import `note` from tests.support.notes "
        "instead:\n  " + "\n  ".join(offenders)
    )
