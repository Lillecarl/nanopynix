r"""Scan the tree for a regular expression that reads an ANSI escape sequence.

Nix writes the escape sequences that reach this library, so Nix owns the answer
to which bytes are an escape sequence. ``nix::filterANSIEscapes`` is that
answer, and ``nanopynix.strip_ansi`` is the way to call it. A pattern written
here is a second answer, and a second answer is wrong as soon as Nix emits a
sequence that it does not cover.

Three of them existed. Each one was a subset of the next, and the widest one
was still narrower than Nix's:

- the ``strip-ansi`` package, ``\x1B\[\d+(;\d+){0,2}m``, an SGR sequence of at
  most three numeric parameters;
- ``nanopynix/tests/primops/test_primop_error_parity.py``,
  ``\x1b\[[0-9;]*m``, any SGR sequence;
- ``nanopynix_helpers.fod``, ``\x1b\[[0-?]*[ -/]*[@-~]``, any CSI sequence.

Measured against three sequences that Nix removes. None of the three patterns
removes an OSC 8 hyperlink, ``\x1b]8;;http://x\x1b\a\x1b]8;;\x1b\``, because
an OSC sequence opens with ``\x1b]`` and each pattern reads ``\x1b[``. The
first one leaves ``\x1b[38;2;255;0;0m`` in place, because a 24-bit colour
carries five parameters and the pattern reads three. The first and the second
one keep ``\x1b[2K``, which does not end in ``m``.

``nanopynix/tests/bindings/test_util_bindings.py`` holds the same three
sequences against the filter that this repository now uses.

**The ban is on re-implementing the filter, not on the escape character.** A
test that builds an escape sequence as a fixture is how the filter gets
tested, so a bare string literal is legal everywhere. Only a pattern that goes
to the ``re`` module counts.

Two limits, stated rather than hidden. The scanner reads the argument of the
call, so a pattern assembled from a module constant passes it. And it knows
the ``re`` module by the name ``re``, so an aliased import passes it. Neither
shape exists here, and both would be a strange way to write the thing that
this rule forbids.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support.suppressions import iter_python_files

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# The functions of the `re` module that take a pattern as their first argument.
# `re.escape` and `re.purge` take no pattern, so they are not here.
RE_FUNCTIONS = frozenset({"compile", "findall", "finditer", "fullmatch", "match", "search", "split", "sub", "subn"})

# How an escape character reaches a pattern. The first entry is the character
# itself, which is what a plain string literal holds. The rest are the source
# spellings that survive into the *value* of a raw string literal, where the
# `re` module decodes them instead of Python.
ESCAPE_SPELLINGS = ("\x1b", "\\x1b", "\\x1B", "\\033", "\\u001b", "\\u001B", "\\e", "\\N{ESC")

# The ledger of files that read an escape sequence themselves, and why each one
# may. A path is here only when `nanopynix.strip_ansi` is the wrong answer, and
# not when calling it is merely inconvenient. Same shape as the ledger in
# `tests/meta/test_consumer_surface.py`, and for the same reason: an exemption
# that a machine records is an exemption a reader can find.
EXEMPT: dict[str, str] = {
    "completion-spike/src/completion_spike/_pty.py": (
        "Not Nix output. This reads what a *shell* draws on a pty -- cursor "
        "movement, an OSC title, and a private-parameter sequence that fish "
        "writes -- so `nix::filterANSIEscapes` is not the authority on it. It "
        "also applies a backspace rather than removing it, which no filter of "
        "Nix log text does. And `completion-spike` is a nixpkgs "
        "`buildPythonApplication` that depends on `cyclopts` alone: importing "
        "`nanopynix` would put the whole library in the runtime closure of a "
        "package whose subject is shell plumbing."
    ),
}


@dataclass(frozen=True)
class Violation:
    """One regular expression that reads an escape sequence itself."""

    path: Path
    line: int
    call: str
    pattern: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.call}({self.pattern!r})"


def _called_name(func: ast.expr) -> str | None:
    """``re.compile`` for ``re.compile(...)``, and ``None`` for anything else."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "re":
        return f"re.{func.attr}" if func.attr in RE_FUNCTIONS else None
    return None


def _literal_parts(node: ast.expr) -> Iterator[str]:
    """Every string constant inside ``node``, including the parts of an f-string."""
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value


def scan_source(source: str, path: Path) -> list[Violation]:
    """Return every escape-reading pattern in ``source``.

    Takes source text rather than a path, so the unit tests can pin both
    directions without writing a file.
    """
    violations: list[Violation] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = _called_name(node.func)
        if name is None:
            continue
        parts = list(_literal_parts(node.args[0]))
        escaping = [part for part in parts if any(spelling in part for spelling in ESCAPE_SPELLINGS)]
        if escaping:
            violations.append(Violation(path, node.lineno, name, escaping[0]))
    return violations


def scan_tree(root: Path) -> list[Violation]:
    """Return every escape-reading pattern under ``root``."""
    violations: list[Violation] = []
    for path in iter_python_files(root):
        relative = path.relative_to(root)
        if relative.as_posix() in EXEMPT:
            continue
        violations.extend(scan_source(path.read_text(encoding="utf-8"), relative))
    return violations


def format_report(violations: Iterable[Violation]) -> str:
    """Render violations one per line, for a test failure."""
    return "\n".join(str(v) for v in violations)
