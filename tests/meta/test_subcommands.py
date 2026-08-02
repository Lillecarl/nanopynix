"""Drift guard for pynix/__init__.py's subcommand-union construction.

`Pynix.subcommand`'s type is built two independent ways that must describe
the same set of concrete Command subclasses: a runtime list
(`_subcommand_types`, reduced via `functools.reduce(operator.or_, ...)`
since `ekn` is an optional runtime dependency) and a `TYPE_CHECKING`-only
static `Union` (since pyright can't type a runtime-computed value as a type
expression at all -- `reportInvalidTypeForm`). Nothing enforces these stay
in sync: add a subcommand to one and forget the other, and pyright silently
checks against a union that no longer matches runtime behavior. This test
parses `__init__.py`'s own source via `ast` to compare the TYPE_CHECKING
union's member names against the runtime list, since the TYPE_CHECKING
branch never actually executes and so isn't otherwise inspectable.
"""

from __future__ import annotations

import ast
import inspect

import pynix


def _type_checking_union_names() -> set[str]:
    """Extract the names in `_PynixSubcommand = A | B | ... | _Ekn`, the
    assignment under `if TYPE_CHECKING:` in pynix/__init__.py."""
    tree = ast.parse(inspect.getsource(pynix))
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == "_PynixSubcommand"
                ):
                    return _union_member_names(stmt.value)
    raise AssertionError("could not find `_PynixSubcommand = ...` under `if TYPE_CHECKING:` in pynix/__init__.py")


def _union_member_names(expr: ast.expr) -> set[str]:
    """Flatten an `A | B | C` BinOp chain of plain names into a name set."""
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        return _union_member_names(expr.left) | _union_member_names(expr.right)
    if isinstance(expr, ast.Name):
        return {expr.id}
    raise AssertionError(f"unexpected union member expression in _PynixSubcommand: {ast.dump(expr)}")


def test_type_checking_subcommand_union_matches_runtime_subcommand_types() -> None:
    """The static TYPE_CHECKING union and the runtime `_subcommand_types`
    list (which is what actually builds `Pynix.subcommand`'s real type via
    `functools.reduce`) must name the same set of Command subclasses.

    `ekn/src` is always on pyright's `extraPaths`, so the TYPE_CHECKING
    union unconditionally includes `_Ekn`/`Ekn` even in environments where
    the runtime `import ekn.cli` fails and `Ekn` never lands in
    `_subcommand_types` -- account for that one intentional asymmetry
    (rather than treating it as drift) by always including "Ekn" on the
    runtime side too, regardless of whether it's actually there.
    """
    type_checking_names = {"Ekn" if name == "_Ekn" else name for name in _type_checking_union_names()}
    runtime_names = {cls.__name__ for cls in pynix._subcommand_types} | {"Ekn"}

    assert type_checking_names == runtime_names
