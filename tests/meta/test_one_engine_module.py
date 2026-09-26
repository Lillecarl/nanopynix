"""Only the engine modules import a compiled Nix engine.

``nanopynix/_engine.py`` imports ``nanopynix_bindings``, and
``nanopynix/_engine_huggorm.py`` imports ``huggorm_bindings``. Every other
module takes the engine from ``_engine``. Before that module existed, 24
modules imported the bindings, public ones included, and two told a Nix error
by its module name. An import that goes around ``_engine`` again is a place
the other engine cannot reach, and nothing else fails when one appears.

``_engine`` imports the same names in both of its branches, so a name that
only one engine answers fails here and not in the huggorm lane.

This checks imports only. It cannot see an engine class reached through an
area module, such as ``nanopynix_expr.EvalState``: such a call site goes
through ``_engine`` and still names the bindings' shape.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.support.suppressions import iter_python_files

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "nanopynix/src/nanopynix"
ENGINE_MODULE = PACKAGE_DIR / "_engine.py"

#: Each engine package, and the one module allowed to import it.
ENGINE_IMPORTERS = {
    "nanopynix_bindings": ENGINE_MODULE,
    "huggorm_bindings": PACKAGE_DIR / "_engine_huggorm.py",
}


def engine_imports(source: str) -> list[tuple[str, int]]:
    """Each import of an engine package in ``source``, with its line."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        found.extend((name.split(".")[0], node.lineno) for name in names if name.split(".")[0] in ENGINE_IMPORTERS)
    return found


def branch_names(source: str) -> tuple[set[str], set[str]]:
    """The names each branch of the engine selection binds.

    The selection is the one module-level ``if`` whose ``else`` imports from
    ``_engine_huggorm``.
    """
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and node.orelse:
            return _bound(node.body), _bound(node.orelse)
    msg = "the engine module has no if/else that selects an engine"
    raise AssertionError(msg)


def _bound(body: list[ast.stmt]) -> set[str]:
    return {alias.asname or alias.name for stmt in body if isinstance(stmt, ast.ImportFrom) for alias in stmt.names}


def test_the_scanner_can_see_the_package() -> None:
    files = list(iter_python_files(PACKAGE_DIR))
    assert len(files) > 20, f"only {len(files)} file(s) under {PACKAGE_DIR}; the path is wrong"
    for package, module in ENGINE_IMPORTERS.items():
        assert module in files, f"{module.name} is not being scanned"
        assert package in {name for name, _ in engine_imports(module.read_text(encoding="utf-8"))}, (
            f"{module.name} imports no {package}"
        )


def test_the_scanner_finds_an_engine_import() -> None:
    assert engine_imports("from nanopynix_bindings import expr\n") == [("nanopynix_bindings", 1)]
    assert engine_imports("from nanopynix_bindings.store import BuildMode\n") == [("nanopynix_bindings", 1)]
    assert engine_imports("import huggorm_bindings.eval\n") == [("huggorm_bindings", 1)]
    assert engine_imports("if TYPE_CHECKING:\n    from nanopynix_bindings.store import Store\n") == [
        ("nanopynix_bindings", 2)
    ]


def test_the_scanner_ignores_what_is_not_an_import() -> None:
    assert engine_imports("from nanopynix._engine import expr\n") == []
    assert engine_imports('"""See ``nanopynix_bindings.store.Store``."""\n') == []
    assert engine_imports("from nanopynix_bindings_extra import x\n") == []


def test_the_branch_reader_sees_both_branches() -> None:
    source = "if X:\n    from a import b as b, c as c\nelse:\n    from d import b as b\n"
    assert branch_names(source) == ({"b", "c"}, {"b"})


def test_only_the_engine_modules_import_an_engine() -> None:
    found = [
        f"{path.relative_to(REPO_ROOT)}:{line}: {package}"
        for path in iter_python_files(PACKAGE_DIR)
        for package, line in engine_imports(path.read_text(encoding="utf-8"))
        if path != ENGINE_IMPORTERS[package]
    ]
    assert not found, (
        "a module of nanopynix imports a Nix engine directly. Import the name from "
        "nanopynix._engine, and add it there, in both branches, if it is missing:\n" + "\n".join(found)
    )


def test_both_engines_answer_the_same_names() -> None:
    bindings, huggorm = branch_names(ENGINE_MODULE.read_text(encoding="utf-8"))
    assert bindings == huggorm, (
        "the two branches of nanopynix/_engine.py import different names. "
        f"Only nanopynix_bindings: {sorted(bindings - huggorm)}. Only huggorm: {sorted(huggorm - bindings)}. "
        "Add the missing name to the other branch; _engine_huggorm may answer it with a placeholder."
    )
