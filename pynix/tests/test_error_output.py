"""``error_exit`` puts a message of Nix on stderr, and keeps its colour.

The subject is one line of :func:`pynix._util.error_exit`. Every test here
toggles that line, rather than asserting the shape of the output alone: the
control renders the same message the way the function rendered it before, and
shows the damage that the current form avoids.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.text import Text

from pynix import _util

# What Nix sends for `error: value is a string`, with its own colour. `\x1b[`
# starts each escape, `31;1m` is bold red, and `0m` ends the run.
NIX_MESSAGE = "\x1b[31;1merror:\x1b[0m \x1b[35;1mvalue is a string\x1b[0m"
PLAIN_WORDS = "error: value is a string"


def _render(message: str | Text, *, terminal: bool, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run ``error_exit`` against a console this test owns, and return what it wrote."""
    buffer = io.StringIO()
    monkeypatch.setattr(_util, "error_console", Console(file=buffer, force_terminal=terminal, width=200))
    with pytest.raises(SystemExit):
        _util.error_exit(message)
    return buffer.getvalue()


def test_a_message_of_nix_keeps_its_colour_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The colour arrives as a style of rich, and the words stay whole."""
    written = _render(NIX_MESSAGE, terminal=True, monkeypatch=monkeypatch)

    # `Text.from_ansi` is the reader here, and not the subject: it turns the
    # escapes back into styles, so `.plain` is what a terminal shows.
    assert PLAIN_WORDS in Text.from_ansi(written).plain
    # Bold magenta, which is what `35;1m` of Nix means. rich writes its own
    # form of the same style, with the attribute first.
    assert "\x1b[1;35m" in written


def test_interpolation_breaks_the_same_message() -> None:
    """The control, and the reason ``error_exit`` takes a ``Text``.

    ``ReprHighlighter`` matches the ``[`` and the ``35`` of the escape and
    styles each one, so the escape byte of Nix ends up in front of a style of
    rich and the words break apart. This is the shape ``error_exit`` used, and
    it must stay a failure.
    """
    buffer = io.StringIO()
    Console(file=buffer, force_terminal=True, width=200).print(f"[red]Error:[/red] {NIX_MESSAGE}")
    written = buffer.getvalue()

    # The escape byte survives with nothing after it, because rich took the
    # bracket for itself.
    assert "\x1b\x1b[1m[" in written
    assert PLAIN_WORDS not in written


def test_no_terminal_drops_the_colour_and_keeps_the_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pynix ... 2> file` reads the words, and no escape."""
    written = _render(NIX_MESSAGE, terminal=False, monkeypatch=monkeypatch)

    assert PLAIN_WORDS in written
    assert "\x1b" not in written


def test_a_message_of_pynix_is_not_read_as_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bracket in a message pynix wrote reaches the reader.

    ``NixError`` renders as ``[TypeError] ...``, and a caller passes that to
    ``error_exit``. A ``Text`` is never markup, so the tag stays text whatever
    rich would otherwise make of it.
    """
    written = _render("[TypeError] value is a string", terminal=False, monkeypatch=monkeypatch)

    assert "[TypeError] value is a string" in written


def test_error_exit_chains_the_cause() -> None:
    """``--debug`` keeps the real reason. See the *cause* parameter."""
    original = ValueError("the real reason")

    with pytest.raises(SystemExit) as caught:
        _util.error_exit("something went wrong", cause=original)

    assert caught.value.__cause__ is original


def test_error_exit_writes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """The module console, and not the one a test substitutes.

    Every other test here replaces ``error_console``, so nothing else proves
    that the real one goes to stderr. stdout carries the answer of a command.
    """
    with pytest.raises(SystemExit):
        _util.error_exit("something went wrong")
    captured = capsys.readouterr()

    assert "something went wrong" in captured.err
    assert captured.out == ""
