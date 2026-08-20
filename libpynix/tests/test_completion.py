"""What ``complete`` does, and the one thing it corrects before it answers.

The pty suite at ``pynix/completions/tests/`` drives fish, bash and zsh
against an installed program and is the proof that a completion reaches a
shell at all. It takes about four minutes. These tests read the same two
answers in milliseconds, so a change to this layer reports here first.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, cast

import pytest
from argcomplete.lexers import (
    split_line,  # type: ignore[reportUnknownVariableType] -- argcomplete carries no annotations for its lexer
)
from argcomplete.packages import _shlex

from libpynix import Command, build_parser, complete, opt
from libpynix.command import _let_a_hash_stay_in_the_line

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Imports the package and completes nothing, then reports what it loaded.
_PROBE = """
import json, os, sys
os.environ.pop("_ARGCOMPLETE", None)
import libpynix
libpynix.complete(libpynix.build_parser(libpynix.Command))
print(json.dumps({"argcomplete": "argcomplete" in sys.modules}))
"""


#: What `split_line` answers with. It carries no annotations, so the shape is
#: stated once here rather than at each call: the quote, the word being
#: completed, the rest of that word, the words before it, and where the last
#: word break was.
type _Lexed = tuple[str, str, str, list[str], int | None]


def lex(line: str) -> tuple[str, list[str]]:
    """The word *line* is completing, and the words before it."""
    _, prefix, _, words, _ = cast("_Lexed", split_line(line, len(line)))
    return prefix, words


class Root(Command):
    """A program."""

    file: str | None = opt(None, short="f", help="A file.")


@pytest.fixture
def lexer_restored() -> Iterator[None]:
    """Put back the lexer class that ``argcomplete`` shipped.

    The correction replaces an attribute of a module, which lasts for the
    process. Every other test in this repository that completes a line would
    otherwise read a lexer that this one installed.
    """
    original = _shlex.shlex
    try:
        yield
    finally:
        _shlex.shlex = original


def test_a_run_that_is_not_a_completion_loads_no_argcomplete() -> None:
    """argcomplete is 39 modules, and a real command needs none of them.

    The variable is set by the generated completion script and by nothing
    else, so this is the path that every command a person types takes. A
    subprocess, because this suite imports argcomplete at its own top.
    """
    result = subprocess.run(  # noqa: S603 -- this interpreter, on a literal program
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["argcomplete"] is False


def test_complete_answers_nothing_when_the_variable_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard for the probe above, read from inside this process."""
    monkeypatch.delenv("_ARGCOMPLETE", raising=False)
    reached: list[str] = []
    monkeypatch.setattr("libpynix.command._let_a_hash_stay_in_the_line", lambda: reached.append("x"))

    complete(build_parser(Root))

    assert reached == []


@pytest.mark.usefixtures("lexer_restored")
def test_a_hash_stays_in_the_line() -> None:
    """A command line is not a script, and no part of one is a comment.

    argcomplete lexes the line with a vendored ``shlex`` whose ``commenters``
    is ``#``, so everything from the first ``#`` was dropped and
    ``prog build --file .#hello --at<TAB>`` completed an empty word. A flake
    reference is the shape a Nix program is typed with most. Issue #221.
    """
    line = "prog build --file .#hello --at"
    assert lex(line) == ("", ["prog", "build", "--file", "."])

    _let_a_hash_stay_in_the_line()

    assert lex(line) == ("--at", ["prog", "build", "--file", ".#hello"])


@pytest.mark.usefixtures("lexer_restored")
def test_the_correction_leaves_an_ordinary_line_alone() -> None:
    """The guard: a lexer that dropped every word would pass the test above."""
    _let_a_hash_stay_in_the_line()

    line = "prog build --file ./default.nix --attr hel"
    assert lex(line) == ("hel", ["prog", "build", "--file", "./default.nix", "--attr"])
