"""Published Python snippets, and the example regions they mirror.

`#23 <https://github.com/Lillecarl/nanopynix/issues/23>`_: no test executed a
fenced code block, and two published snippets did not work. ``README.md``
passed ``Session(config=...)``, and no engine has ever had a ``config``
parameter.

``docs/examples/*_example.py`` already runs under
``nanopynix/tests/test_examples.py``. This module joins the two, so a published
block is a **view of code that runs** rather than a second copy of it.

**Why a mirror and not ``literalinclude``.** #23 suggests including each block
by literal reference. Sphinx would do that, and ``README.md`` is not a Sphinx
page -- GitHub renders it, and GitHub does not process directives. A mirror
works in both, and it keeps every page readable on GitHub, so the repository
gets one mechanism instead of two.

The pieces:

* An example file marks a region with ``# region: <name>`` and
  ``# endregion: <name>``. The region is ordinary code inside a working
  program, so it runs with the rest of the file.
* A page puts ``<!-- example: <file>#<name> -->`` immediately before a fenced
  ``python`` block. The comment is invisible in both renderers.
* :func:`region` returns the marked lines, dedented.
  ``tests/meta/test_doc_snippets.py`` asserts that the block equals them.

Dedenting is what lets a page show four lines from the middle of an ``async
def main()``. Without it every snippet would have to be a whole program, and
the pages would be mostly boilerplate.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# A fence that opens a Python block. Only these are checked -- a `console`,
# `nix` or `text` block is prose, and running it is not a thing that means
# anything.
_PYTHON_FENCE = re.compile(r"^([ \t]*)```(python|py)\s*$")
_CLOSING_FENCE = re.compile(r"^[ \t]*```\s*$")

# `<!-- example: settings_example.py#globals -->`, on the line before a fence.
_POINTER = re.compile(r"^<!--\s*example:\s*(?P<file>[\w./-]+)#(?P<name>[\w-]+)\s*-->\s*$")

_REGION_START = re.compile(r"^\s*#\s*region:\s*(?P<name>[\w-]+)\s*$")
_REGION_END = re.compile(r"^\s*#\s*endregion:\s*(?P<name>[\w-]+)\s*$")

EXAMPLES_DIR = "docs/examples"


@dataclass(frozen=True)
class Snippet:
    """One fenced Python block, and the example region it points at."""

    path: Path
    line: int
    text: str
    example: str | None
    region: str | None

    @property
    def pointer(self) -> str | None:
        if self.example is None or self.region is None:
            return None
        return f"{self.example}#{self.region}"

    def __str__(self) -> str:
        return f"{self.path}:{self.line}"


def iter_snippets(paths: list[Path], repo_root: Path) -> Iterator[Snippet]:
    """Every fenced Python block in ``paths``, with its pointer comment if any.

    The pointer must be the line directly above the fence. A blank line
    between them is not allowed, because "somewhere above" is not a rule a
    reader can apply.
    """
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            fence = _PYTHON_FENCE.match(lines[index])
            if fence is None:
                index += 1
                continue
            body: list[str] = []
            cursor = index + 1
            while cursor < len(lines) and not _CLOSING_FENCE.match(lines[cursor]):
                body.append(lines[cursor])
                cursor += 1
            pointer = _POINTER.match(lines[index - 1]) if index > 0 else None
            yield Snippet(
                path=path.relative_to(repo_root),
                line=index + 1,
                text="\n".join(body).strip("\n"),
                example=pointer.group("file") if pointer else None,
                region=pointer.group("name") if pointer else None,
            )
            index = cursor + 1


def region(example: Path, name: str) -> str | None:
    """The dedented lines of ``name`` in ``example``, or ``None`` when absent."""
    lines = example.read_text(encoding="utf-8").splitlines()
    collected: list[str] = []
    inside = False
    for line in lines:
        end = _REGION_END.match(line)
        if inside and end is not None and end.group("name") == name:
            return textwrap.dedent("\n".join(collected)).strip("\n")
        if inside:
            collected.append(line)
            continue
        start = _REGION_START.match(line)
        if start is not None and start.group("name") == name:
            inside = True
    return None


def region_names(example: Path) -> list[str]:
    """Every region ``example`` opens, in file order, including unclosed ones."""
    return [
        match.group("name")
        for match in (_REGION_START.match(line) for line in example.read_text(encoding="utf-8").splitlines())
        if match is not None
    ]


def documented_pages(repo_root: Path) -> list[Path]:
    """Every Markdown page a reader can reach: the README and the docs tree.

    ``docs/examples`` is excluded. A page there would be documenting the
    example files with the example files, which proves nothing.
    """
    pages = [repo_root / "README.md"]
    pages.extend(sorted(p for p in (repo_root / "docs").rglob("*.md") if EXAMPLES_DIR not in p.as_posix()))
    return [p for p in pages if p.is_file()]
