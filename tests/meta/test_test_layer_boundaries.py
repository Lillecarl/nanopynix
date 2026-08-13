"""The two test layers of issue #130 keep their order.

``test_support`` is the generic layer. It holds the deadline, the hang report,
the Git fixtures, the subprocess runner and the note shim, and it names no Nix
concept. ``nanopynix_testing`` is the layer above it: the fixtures, the markers
and the soak driver, all of which build a real store.

**The order is the whole point of the split, and prose alone did not keep it.**
The issue asked for a subproject to reach the generic layer without reaching
nanopynix's, and ``grpclib-transports`` is what demonstrates that: its
``pytest.ini`` registers ``test_support.plugin`` and nothing else. One import
of ``nanopynix`` from ``test_support`` makes every one of those suites build
the bindings, and nothing else in this repository reports it. ``nix/checks.nix``
builds the suite of ``test-support`` in a venv that holds no nanopynix, which
catches an import that *runs*; this gate also catches one under
``if TYPE_CHECKING:``, and it names the line.

The rule below is one direction only. ``nanopynix_testing`` imports
``test_support`` freely, because that is what a layer above is for.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.support.suite_roots import HELPER_ROOTS, REPO_ROOT, SUITE_ROOTS, check_roster

GENERIC_LAYER = REPO_ROOT / "test-support" / "src" / "test_support"
NIX_LAYER = REPO_ROOT / "nanopynix-testing" / "src" / "nanopynix_testing"

# What the generic layer must not reach. Every one of these either loads Nix,
# or is a suite that sits on top of both layers.
_FORBIDDEN_FOR_GENERIC = (
    "nanopynix",
    "nanopynix_testing",
    "nanopynix_helpers",
    "nanopynix_proto",
    "pynix",
    "ekn",
    "tests",
)

# What the Nix layer must not reach: a suite. A helper that imports the suite
# it serves inverts the dependency, and the suite then cannot move without the
# helper moving too -- which is the state that issue #130 started from.
_FORBIDDEN_FOR_NIX = ("tests",)


def _imported_roots(path: Path) -> set[str]:
    """The first component of every module this file imports.

    ``ast`` and not a regular expression, so an import inside a function or
    under ``if TYPE_CHECKING:`` counts the same as one at the top. A relative
    import names no root and is therefore inside the same package.
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _modules_of(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _offenders(root: Path, forbidden: tuple[str, ...]) -> list[str]:
    return [
        f"{path.relative_to(REPO_ROOT)} imports {name}"
        for path in _modules_of(root)
        for name in sorted(_imported_roots(path) & set(forbidden))
    ]


def test_both_layers_hold_modules_to_scan() -> None:
    """A scanner pointed at an empty directory passes and enforces nothing."""
    check_roster()
    for root in (GENERIC_LAYER, NIX_LAYER):
        assert _modules_of(root), f"{root.relative_to(REPO_ROOT)} holds no module to scan"


def test_the_scanner_reads_an_import_that_a_regular_expression_would_miss(tmp_path: Path) -> None:
    """The two shapes that made ``ast`` worth the cost, rather than a pattern."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from nanopynix.inproc import Session\n"
        "def build() -> None:\n"
        "    import nanopynix_testing.fixtures\n",
        encoding="utf-8",
    )
    assert {"nanopynix", "nanopynix_testing"} <= _imported_roots(sample)


def test_the_generic_layer_names_no_nix_concept() -> None:
    offenders = _offenders(GENERIC_LAYER, _FORBIDDEN_FOR_GENERIC)
    assert not offenders, (
        "test_support is the layer that a suite with no Nix in it opts into, and these imports "
        "take that away:\n  " + "\n  ".join(offenders) + "\n"
        "Move the helper to nanopynix-testing, which is the layer above."
    )


def test_the_nix_layer_imports_no_suite() -> None:
    offenders = _offenders(NIX_LAYER, _FORBIDDEN_FOR_NIX)
    assert not offenders, (
        "nanopynix_testing is installed, and a suite is not, so these imports resolve only from "
        "the repository rootdir:\n  " + "\n  ".join(offenders) + "\n"
        "That is the state issue #130 started from, and it is what stops a package leaving."
    )


@pytest.mark.parametrize("root", SUITE_ROOTS + HELPER_ROOTS, ids=lambda root: str(root.name))
def test_no_module_imports_a_suite_of_another_project(root: Path) -> None:
    """``tests`` resolves from the repository rootdir and from nowhere else.

    A module of one project's suite that imports ``tests.support.<name>``
    passes under a run from the repository root and fails under that project's
    own gate, which is the split that issue #130 had to remove. The repository
    suite itself is exempt, because ``tests`` is its own rootdir.
    """
    if root == REPO_ROOT / "tests":
        pytest.skip("`tests` is this suite's own package, so importing it is not a cross-project import")
    offenders = _offenders(root, ("tests",))
    assert not offenders, (
        "these modules import the repository suite, which resolves only from the repository "
        "rootdir:\n  " + "\n  ".join(offenders)
    )
