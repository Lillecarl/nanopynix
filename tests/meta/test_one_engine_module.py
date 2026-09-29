"""Only the engine module imports the compiled Nix engine.

``nanopynix/_engine.py`` imports ``huggorm_bindings``, and every other module
takes the engine from it. An import that goes around it is a second place
that knows the engine, and nothing else fails when one appears.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.support.suppressions import iter_python_files

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "nanopynix/src/nanopynix"

#: The engine package, and the one module allowed to import it.
ENGINE_PACKAGE = "huggorm_bindings"
ENGINE_IMPORTER = PACKAGE_DIR / "_engine.py"


def engine_imports(source: str) -> list[int]:
    """The line of each import of the engine package in ``source``."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        found.extend(node.lineno for name in names if name.split(".")[0] == ENGINE_PACKAGE)
    return found


def test_the_scanner_can_see_the_package() -> None:
    files = list(iter_python_files(PACKAGE_DIR))
    assert len(files) > 20, f"only {len(files)} file(s) under {PACKAGE_DIR}; the path is wrong"
    assert ENGINE_IMPORTER in files, f"{ENGINE_IMPORTER.name} is not being scanned"
    assert engine_imports(ENGINE_IMPORTER.read_text(encoding="utf-8")), f"{ENGINE_IMPORTER.name} imports no engine"


def test_the_scanner_finds_an_engine_import() -> None:
    assert engine_imports("from huggorm_bindings import Store\n") == [1]
    assert engine_imports("import huggorm_bindings.eval\n") == [1]
    assert engine_imports("if TYPE_CHECKING:\n    from huggorm_bindings.errors import NixError\n") == [2]


def test_the_scanner_ignores_what_is_not_an_import() -> None:
    assert engine_imports("from nanopynix._engine import expr\n") == []
    assert engine_imports('"""See ``huggorm_bindings.Store``."""\n') == []
    assert engine_imports("from huggorm_bindings_extra import x\n") == []


def test_only_the_engine_module_imports_the_engine() -> None:
    found = [
        f"{path.relative_to(REPO_ROOT)}:{line}"
        for path in iter_python_files(PACKAGE_DIR)
        if path != ENGINE_IMPORTER
        for line in engine_imports(path.read_text(encoding="utf-8"))
    ]
    assert not found, (
        "a module of nanopynix imports huggorm_bindings directly. Import the name from "
        "nanopynix._engine, and add it there if it is missing:\n" + "\n".join(found)
    )
