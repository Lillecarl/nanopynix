"""Resolve the autodoc directives in ``docs/nanopynix/api/`` to objects.

``tests/meta/test_docs_coverage.py`` is where this runs, and it answers one
question: which name in ``nanopynix.__all__`` has no page.

**A text search is the wrong tool, and it reports the wrong number.** The docs
use Sphinx autodoc, so ``.. automodule:: nanopynix.exceptions`` with
``:members:`` documents every exception class without naming one of them in the
Markdown. Searching the files for each name reports scores of false absences.
This module resolves each directive to the object it renders instead, which is
the same thing Sphinx does when it builds the page.

Matching is by object identity first, and by dotted name second. Identity is
what makes a re-export work: ``nanopynix.StorePath`` and
``nanopynix.models.StorePath`` are one class, so a directive naming either one
documents both. The name check catches what has no stable identity -- a tuple
constant, or a type alias -- where ``id()`` is not a reliable answer.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# The directives that render an object. `autodata` and `autoattribute` are
# included because a module constant is a public name like any other.
DIRECTIVE = re.compile(r"^\.\.\s+(automodule|autoclass|autofunction|autodata|autoattribute)::\s+(?P<target>\S+)\s*$")

# `:members:` on an `automodule` pulls in the module's public attributes. Only
# an option line may follow the directive, so a short lookahead is enough.
_OPTION_LOOKAHEAD = 6

DOCS_API_DIR = "docs/nanopynix/api"


@dataclass(frozen=True)
class Directive:
    """One autodoc directive, and the object it renders."""

    path: Path
    line: int
    kind: str
    target: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: .. {self.kind}:: {self.target}"


def resolve(dotted: str) -> object | None:
    """Resolve a dotted path to an object, or ``None`` when it does not exist.

    The split between module and attribute is not knowable from the text --
    ``nanopynix.rpc.Store`` could be a class in ``nanopynix.rpc`` or a module.
    So the longest importable prefix wins, and the rest is attribute access.
    """
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        obj: object = module
        for attr in parts[cut:]:
            obj = getattr(obj, attr, _MISSING)
            if obj is _MISSING:
                return None
        return obj
    return None


_MISSING = object()


def iter_directives(docs_dir: Path) -> Iterator[Directive]:
    """Every autodoc directive under ``docs_dir``, in file order."""
    for path in sorted(docs_dir.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            match = DIRECTIVE.match(line.strip())
            if match is None:
                continue
            yield Directive(path, index + 1, match.group(1), match.group("target"))


def _has_members_option(lines: list[str], index: int) -> bool:
    return any(":members:" in line for line in lines[index : index + _OPTION_LOOKAHEAD])


def module_members(module: object, dotted: str) -> list[str]:
    """The names ``automodule`` with ``:members:`` renders for ``module``.

    This mirrors Sphinx rather than listing everything reachable. Sphinx uses
    ``__all__`` when the module defines one, and otherwise takes the public
    attributes that the module itself defines. **It does not document an
    imported member** unless ``:imported-members:`` asks for it.

    Reading ``dir()`` instead is wrong twice over. It counts a re-export as
    documented when no page renders it, and in this repository the answer
    would depend on beartype: the ``if TYPE_CHECKING or BEARTYPING:`` blocks
    run under the test suite and not under a plain interpreter, so ``dir()``
    returns more names in pytest than outside it. One name flipped between
    documented and undocumented for exactly that reason.
    """
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return [name for name in declared if not name.startswith("_")]
    return [
        name
        for name in dir(module)
        if not name.startswith("_") and getattr(getattr(module, name, None), "__module__", None) == dotted
    ]


@dataclass
class Documented:
    """What the directives render, in both forms the caller needs.

    ``objects`` is not a convenience -- it is what keeps ``ids`` correct.
    ``getattr`` on a descriptor can build a temporary, and once that temporary
    is collected CPython is free to hand its address to something else. An
    ``id()`` set built without holding the objects therefore reports false
    matches, and did: one name flipped between documented and undocumented
    across two runs of the same code. Holding the references pins the answer.
    """

    objects: list[object]
    ids: set[int]
    names: set[str]


def documented(docs_dir: Path) -> Documented:
    """Return everything the directives under ``docs_dir`` document.

    An ``automodule`` with ``:members:`` contributes each public attribute of
    that module, because that is what Sphinx renders.
    """
    objects: list[object] = []
    names: set[str] = set()
    for path in sorted(docs_dir.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            match = DIRECTIVE.match(line.strip())
            if match is None:
                continue
            target = match.group("target")
            names.add(target)
            obj = resolve(target)
            if obj is None:
                continue
            objects.append(obj)
            if match.group(1) != "automodule" or not _has_members_option(lines, index + 1):
                continue
            for attr in module_members(obj, target):
                member = getattr(obj, attr, None)
                if member is None:
                    continue
                objects.append(member)
                names.add(f"{target}.{attr}")
    return Documented(objects, {id(o) for o in objects}, names)
