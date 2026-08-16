"""The two lists of `pynix` subcommands must name the same commands.

`pynix/src/pynix/__init__.py` builds `_PynixSubcommand` twice. The runtime half
starts from `_SUBCOMMANDS`, appends `Lsp` when `pynix-lsp` imports, and folds
the list with `|`. The static half, under `if TYPE_CHECKING:`, writes the union
out by hand with every member in it.

**Two halves, because neither one alone works.** clypi eval()s the annotation
of `Pynix.subcommand` against the globals of the module while the class body
runs, so an optional member cannot be spliced in after the fact -- the runtime
half has to exist. pyright cannot read a value that a `for` loop computed as a
type expression (`reportInvalidTypeForm`), so the static half has to exist too.
Issue #107 split the language server out of `pynix` and made the first member
optional, which is what created the second list.

**Nothing else compares them.** A subcommand added to one half and not the
other has no symptom in the half that has it: the program runs, the type
checker is happy, and only the other view of the CLI is short one command.

The second test is the other end of the same edge. The optional import in the
runtime half is a `try`/`except ImportError`, and an import that silently never
succeeds looks exactly like a language server that is not installed. This suite
runs in the dev shell, which installs `pynix-lsp`, so `lsp` must be mounted
here. `checks.pynix-isolated` in `nix/checks.nix` states the opposite case, in
a venv that holds `pynix` alone.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import pynix

SOURCE = Path(pynix.__file__)


def _union_members(node: ast.expr) -> list[str]:
    """Flatten a chain of `A | B | C` into the names it joins."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _union_members(node.left) + _union_members(node.right)
    if isinstance(node, ast.Name):
        return [node.id]
    raise AssertionError(f"the static union holds something that is not a name: {ast.dump(node)}")


def _type_checking_branch() -> ast.If:
    module = ast.parse(SOURCE.read_text())
    for node in module.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            return node
    raise AssertionError(f"{SOURCE} no longer has an `if TYPE_CHECKING:` block at module level")


def _static_members(branch: ast.If) -> list[str]:
    for node in branch.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_PynixSubcommand" for t in node.targets
        ):
            return _union_members(node.value)
    raise AssertionError("the `if TYPE_CHECKING:` block no longer assigns `_PynixSubcommand`")


def _runtime_members(branch: ast.If) -> list[str]:
    """Every member of `_SUBCOMMANDS`, the unconditional ones and the appended ones."""
    names: list[str] = []
    for node in ast.walk(ast.Module(body=branch.orelse, type_ignores=[])):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_SUBCOMMANDS" for t in node.targets
        ):
            if not isinstance(node.value, ast.List):
                raise AssertionError("`_SUBCOMMANDS` is no longer a list literal")
            for element in node.value.elts:
                assert isinstance(element, ast.Name), (
                    f"`_SUBCOMMANDS` holds something that is not a name: {ast.dump(element)}"
                )
                names.append(element.id)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_SUBCOMMANDS"
        ):
            names += [a.id for a in node.args if isinstance(a, ast.Name)]
    if not names:
        raise AssertionError("the `else:` branch no longer builds `_SUBCOMMANDS`")
    return names


def test_the_two_unions_name_the_same_subcommands() -> None:
    branch = _type_checking_branch()
    static = _static_members(branch)
    runtime = _runtime_members(branch)

    assert sorted(static) == sorted(runtime), (
        "the static union under `if TYPE_CHECKING:` and the runtime `_SUBCOMMANDS` list "
        f"disagree.\nonly static: {sorted(set(static) - set(runtime))}\n"
        f"only runtime: {sorted(set(runtime) - set(static))}"
    )
    assert len(set(static)) == len(static), f"the static union repeats a member: {static}"


def test_the_optional_import_really_mounts_the_language_server() -> None:
    pytest.importorskip("pynix_lsp", reason="the language server is a separate project since issue #107")

    assert "lsp" in pynix.Pynix.subcommands(), (
        "`pynix-lsp` is installed, so `pynix/__init__.py` must have mounted `Lsp`. "
        "An `except ImportError` that swallows a real failure looks the same as an absent server."
    )
