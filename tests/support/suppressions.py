"""Scan the tree for lint/type suppressions that do not say why they exist.

CLAUDE.md requires every suppression to name its rule *and* justify itself:
``# type: ignore[rule] -- reason`` / ``# noqa: RULE -- reason``. Ruff's
PGH003/PGH004 enforce the first half -- that a bare ``# type: ignore`` or
``# noqa`` names codes -- and nothing enforces the second half, nor do they
cover ``# pyright: ignore[...]`` at all. This module is that missing half.

Why here rather than in a linter. Ruff has no plugin API. The rule is purely
lexical -- it is about comment text, not about the program -- so the engines
that could express it (pylint, ast-grep, semgrep) would all be paying for a
syntax tree this never reads. It is also the one rule that has to fail
*closed*: an inference-based checker that cannot resolve an import silently
passes, and a gate that quietly no-ops is worse than no gate. So: tokenize,
which sees exactly what the rule is about, and a hard error on anything it
cannot read.

`tokenize` rather than a regex over lines because a ``#`` inside a string
literal is not a comment, and a rule that fires on ``"# type: ignore"`` in a
docstring would be trained away rather than fixed.

Two grammars, because the directives really are two different things:

Inline directives suppress one line -- ``# noqa``, ``# type: ignore[...]``,
``# pyright: ignore[...]``. Their justification must be on that line, after a
spaced ``--``. Note that everything from the first ``#`` to end-of-line is a
single comment token, so the common chained form

    value = obj._private  # type: ignore[reportPrivateUsage] -- cross-class access  # noqa: SLF001

is one token carrying one justification, and passes. That is deliberate: the
two directives are the same suppression seen by two tools, and demanding the
reason twice would be noise. The trailing bare ``# noqa`` cannot stand alone --
strip the prose and the line fails.

File-level directives suppress a whole file -- ``# ruff: noqa``,
``# pyright: rule=false``, ``# mypy: ...``. Because they are broader they may
justify themselves either inline after ``--`` or in the comment lines directly
beneath, which is the shape the codebase already uses:

    # pyright: reportPrivateUsage=false, reportUnknownMemberType=false
    # The entire file exercises pool/session internals via mock access.

``# pyright:`` is both kinds depending on what follows it, so it is classified
by whether the word after the colon is ``ignore``.
"""

from __future__ import annotations

import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# Directories that are not first-party source: build outputs, caches, and the
# scratch trees. `result*` are nix build symlinks, which may dangle.
#
# **`pynixd` is first-party source and it is here anyway.** That tree arrived
# by a merge of two histories that changed no file, so it still carries the
# conventions of the repository it came from: 137 of its suppressions do not
# have the ` -- <reason>` this scanner asks for. Reporting all 137 says that
# tree is wrong, and what is true is that a second project has not adopted
# this convention yet. Issue #131 is the work that adopts it, and this entry
# leaves with that work.
_SKIP_DIRS = frozenset(
    {
        ".direnv",
        ".git",
        ".jj",
        ".pytest-agent",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "pynixd",
        "tmp",
    }
)

# The directive keywords, in the two forms that matter.
#
# Detecting these by "does the comment contain the text" does not work, and
# failed on this very file: a comment that *documents* the convention quotes a
# directive verbatim, backticks and all, and the quoted copy is character-for-
# character a real one. Anchoring to a preceding `#` does not help either --
# the quote includes the `#`.
#
# What separates a directive from prose about a directive is position. A real
# inline suppression either trails code on its line (that is the line it
# suppresses) or opens its own comment. Prose describing one does neither: it
# mentions the directive partway through a sentence on a standalone line. So
# `_ANYWHERE` is only consulted for comments that trail code, and `_AT_START`
# for the rest. The residual gap -- a standalone comment whose first words are
# a bare directive followed by prose about it -- is left flagged on purpose:
# that comment is ambiguous to a human reader too, and reads better reworded.
_INLINE_ANYWHERE = re.compile(r"#\s*(?:noqa\b|type:\s*ignore\b|pyright:\s*ignore\b)")
_INLINE_AT_START = re.compile(r"^#\s*(?:noqa\b|type:\s*ignore\b|pyright:\s*ignore\b)")

# A whole-file directive, which is only ever a standalone comment that opens
# with the pragma. `ruff: noqa` and `mypy:` are unambiguous; `pyright:` is
# file-level only when it is *not* followed by `ignore[...]`.
#
# The hashes are omitted from those names on purpose. Spelling the first one
# in full made ruff itself report "Invalid `ruff: noqa` directive" against this
# comment -- it has the very false-positive problem described above, and reads
# prose about a file-level pragma as a malformed pragma.
_FILE_LEVEL_AT_START = re.compile(r"^#\s*(?:ruff:\s*noqa\b|mypy:|pyright:\s*(?!ignore\b)\w)")

# The justification marker: a spaced `--` followed by something. Spaced so that
# a `--flag` inside a documented command line is not mistaken for one.
_JUSTIFIED = re.compile(r"\s--\s+\S")


@dataclass(frozen=True)
class Violation:
    """One suppression that does not explain itself."""

    path: Path
    line: int
    kind: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.kind}] {self.text.strip()}"


def _iter_comments(source: str) -> Iterator[tuple[tokenize.TokenInfo, bool]]:
    """Yield every COMMENT token in ``source``, paired with "trails code".

    Raises rather than skipping on unreadable input: a file this cannot parse
    is a file whose suppressions went unchecked, and that must be loud.
    """
    readline = io.StringIO(source).readline
    # Only these can precede a comment that opens its own line. Anything else
    # on the same row means the comment trails real code.
    layout = {tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING}
    previous: tokenize.TokenInfo | None = None
    for token in tokenize.generate_tokens(readline):
        if token.type == tokenize.COMMENT:
            trails_code = previous is not None and previous.type not in layout and previous.end[0] == token.start[0]
            yield token, trails_code
        previous = token


def scan_source(source: str, path: Path) -> list[Violation]:
    """Return every unjustified suppression in ``source``."""
    comments = list(_iter_comments(source))
    # Line number -> comment text, so a file-level directive can look at the
    # comment lines directly beneath it for its justification.
    by_line = {token.start[0]: token.string for token, _ in comments}

    violations: list[Violation] = []
    for token, trails_code in comments:
        text = token.string
        line = token.start[0]

        if not trails_code and _FILE_LEVEL_AT_START.search(text):
            if _JUSTIFIED.search(text):
                continue
            # A run of comment lines immediately below counts, provided at
            # least one of them is prose rather than another directive.
            probe = line + 1
            explained = False
            while (below := by_line.get(probe)) is not None:
                if not _FILE_LEVEL_AT_START.search(below) and not _INLINE_AT_START.search(below):
                    explained = True
                    break
                probe += 1
            if not explained:
                violations.append(Violation(path, line, "file-level", text))
            continue

        found = _INLINE_ANYWHERE.search(text) if trails_code else _INLINE_AT_START.search(text)
        if found and not _JUSTIFIED.search(text):
            violations.append(Violation(path, line, "inline", text))

    return violations


def iter_python_files(root: Path) -> Iterator[Path]:
    """Yield first-party ``.py`` files under ``root``."""
    for path in sorted(root.rglob("*.py")):
        parts = set(path.parts)
        if parts & _SKIP_DIRS or any(p.startswith("result") for p in path.parts):
            continue
        yield path


def scan_tree(root: Path) -> list[Violation]:
    """Return every unjustified suppression under ``root``."""
    violations: list[Violation] = []
    for path in iter_python_files(root):
        violations.extend(scan_source(path.read_text(encoding="utf-8"), path.relative_to(root)))
    return violations


def format_report(violations: Iterable[Violation]) -> str:
    """Render violations one per line, for a test failure or the CLI."""
    return "\n".join(str(v) for v in violations)
