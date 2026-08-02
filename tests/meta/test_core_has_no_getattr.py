"""No class in ``nanopynix/_core/`` forwards an unlisted name to a binding.

``CoreStore.__getattr__`` went first, and ``CoreEvalState.__getattr__`` was
the last one (#16). Both did the same thing: return ``getattr(self.raw, name)``
typed as ``Any``, so every call through them was a call pyright could not
check. The core is the layer that wraps the compiled bindings, and a hole here
propagates to everything above it.

**This gate covers the smaller half of the problem, and says so rather than
implying otherwise.** Deleting ``CoreEvalState.__getattr__`` produced zero
pyright errors, which looked like proof that nothing used it. It was not. Four
call sites in ``rpc/worker/_worker_eval.py`` reached ``repl_active``,
``begin_repl``, ``repl_scope_names`` and ``reset_file_cache`` through a helper
annotated ``-> Any``, and an ``Any`` stops the check before it reaches the
attribute. Typing that helper turned the four into pyright errors, and each
one became a typed method.

An ``Any`` return is not gateable the way ``__getattr__`` is: this repository
uses ``Any`` deliberately for a proto message and for a dispatch payload, so a
rule against it would be noise. What is left is a habit rather than a check --
when a wrapper reports no callers, look for an ``Any`` between it and them
before concluding it has none.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.support.suppressions import iter_python_files

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_DIR = REPO_ROOT / "nanopynix/src/nanopynix/_core"


def forwarding_classes(source: str) -> list[tuple[str, int]]:
    """Each class in ``source`` that defines ``__getattr__``, with its line.

    Takes source text rather than a path, so the unit test below can pin both
    directions without writing a file.
    """
    return [
        (node.name, item.lineno)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef)
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name == "__getattr__"
    ]


def test_the_scanner_can_see_the_core() -> None:
    """Fail loudly if the gate is pointed somewhere with no source in it."""
    assert CORE_DIR.is_dir(), f"{CORE_DIR} is missing; is the source tree present?"
    files = list(iter_python_files(CORE_DIR))
    assert len(files) > 3, f"only {len(files)} file(s) under {CORE_DIR}; the path is wrong"
    assert any(p.name == "_objects.py" for p in files), "the module this rule is about is not being scanned"


def test_the_scanner_finds_a_getattr() -> None:
    """Both directions, so the gate cannot pass by matching nothing."""
    assert forwarding_classes("class C:\n    def __getattr__(self, name: str) -> object: ...\n") == [("C", 2)]
    assert forwarding_classes("class C:\n    def get(self, name: str) -> object: ...\n") == []


def test_the_scanner_ignores_a_module_level_getattr() -> None:
    """PEP 562's module ``__getattr__`` is a different thing, and is allowed.

    It backs a lazy re-export rather than an untyped forward to a binding, and
    a type checker follows it through the stub. Only a class body counts here.
    """
    assert forwarding_classes("def __getattr__(name: str) -> object: ...\n") == []


def test_no_core_class_forwards_an_unlisted_name() -> None:
    found = [
        f"{path.relative_to(REPO_ROOT)}:{line}: {name}.__getattr__"
        for path in iter_python_files(CORE_DIR)
        for name, line in forwarding_classes(path.read_text(encoding="utf-8"))
    ]
    assert not found, (
        "a class in nanopynix/_core/ defines __getattr__, which forwards an unlisted name to a "
        "compiled binding and returns Any. Give the call a typed method instead:\n" + "\n".join(found)
    )
