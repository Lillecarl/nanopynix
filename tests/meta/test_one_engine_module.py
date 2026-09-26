"""Only ``nanopynix/_engine.py`` imports the compiled Nix engine.

huggorm's generated bindings are to replace ``nanopynix_bindings`` as a
build-time choice, behind that one module. Before it existed, 24 modules of
the package imported the bindings, public ones included, and two told a Nix
error by its module name. An import that goes around ``_engine`` again is a
place the other engine cannot reach, and nothing else fails when one appears.

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
ENGINE_PACKAGE = "nanopynix_bindings"


def engine_imports(source: str) -> list[int]:
    """The line of each import of the engine package in ``source``."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        if any(name.split(".")[0] == ENGINE_PACKAGE for name in names):
            lines.append(node.lineno)
    return lines


def test_the_scanner_can_see_the_package() -> None:
    files = list(iter_python_files(PACKAGE_DIR))
    assert len(files) > 20, f"only {len(files)} file(s) under {PACKAGE_DIR}; the path is wrong"
    assert ENGINE_MODULE in files, "the engine module is not being scanned"
    assert engine_imports(ENGINE_MODULE.read_text(encoding="utf-8")), "the engine module imports no engine"


def test_the_scanner_finds_an_engine_import() -> None:
    assert engine_imports("from nanopynix_bindings import expr\n") == [1]
    assert engine_imports("from nanopynix_bindings.store import BuildMode\n") == [1]
    assert engine_imports("import nanopynix_bindings.util\n") == [1]
    assert engine_imports("if TYPE_CHECKING:\n    from nanopynix_bindings.store import Store\n") == [2]


def test_the_scanner_ignores_what_is_not_an_import() -> None:
    assert engine_imports("from nanopynix._engine import expr\n") == []
    assert engine_imports('"""See ``nanopynix_bindings.store.Store``."""\n') == []
    assert engine_imports("from nanopynix_bindings_extra import x\n") == []


def test_only_the_engine_module_imports_the_engine() -> None:
    found = [
        f"{path.relative_to(REPO_ROOT)}:{line}"
        for path in iter_python_files(PACKAGE_DIR)
        if path != ENGINE_MODULE
        for line in engine_imports(path.read_text(encoding="utf-8"))
    ]
    assert not found, (
        f"a module of nanopynix imports {ENGINE_PACKAGE} directly. Import the name from "
        "nanopynix._engine, and add it there if it is missing:\n" + "\n".join(found)
    )
