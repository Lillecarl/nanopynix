"""Tests for the Markdown renderer that the REPL and `search` share.

A NixOS option description is MyST Markdown, and Rich knows CommonMark. Each
test here pins one place where the two disagree, and each one comes from a real
description rather than from an invented string.
"""

from __future__ import annotations

import os

import pytest
from prompt_toolkit.formatted_text import ANSI, to_formatted_text

import pynix._markdown as markdown_module
from pynix._markdown import MEASURE, NixMarkdown, render_markdown


def _text(markup: str, width: int = 78) -> str:
    """The visible text that the renderer produces, with the styles removed."""
    rendered: ANSI = render_markdown(markup, width)
    return "".join(fragment[1] for fragment in to_formatted_text(rendered))


def test_an_autolink_shows_its_address_once() -> None:
    """`<https://...>` is what nixpkgs writes, and its text is the address.

    Regression test. Rich prints the address after the text when hyperlinks
    are off, so an autolink showed the address twice. One option of
    home-manager carries an 88-character URL, and the repeat filled four lines
    of the detail pane rather than two.
    """
    text = _text("See <https://example.com/a/b> for more.")
    assert text.count("https://example.com/a/b") == 1
    assert "(" not in text


def test_a_named_link_keeps_its_address() -> None:
    """A terminal cannot follow a link, so the address has to be readable."""
    text = _text("See [the docs](https://example.com/a/b) for more.")
    assert "the docs" in text
    assert "https://example.com/a/b" in text


def test_no_terminal_hyperlink_escape_reaches_the_output() -> None:
    """Rich writes a link as an OSC 8 escape, and `ANSI` cannot read one.

    Regression test. `prompt_toolkit.ANSI` reads a CSI escape and not an OSC
    one, so it dropped the escape byte and printed the rest of the sequence:
    a description that named a URL showed `8;id=16117648;https://...` on the
    screen. The test reads the string that Rich produces, before `ANSI` parses
    it, because the parser is what destroys the evidence.
    """
    rendered = render_markdown("See <https://example.com/a/b> now.", 78)
    assert "\x1b]8" not in rendered.value
    assert "8;id=" not in _text("See <https://example.com/a/b> now.")


def test_a_myst_role_shows_its_content() -> None:
    """nixpkgs writes a cross reference as a role, and Rich drops the token.

    Regression test. The description of `_module.args` read
    "• : The nixpkgs package set according to the  option.", because
    ``{var}`pkgs``` and ``{option}`nixpkgs.pkgs``` each rendered as nothing.
    """
    text = _text("- {var}`pkgs`: The set according to the {option}`nixpkgs.pkgs` option.")
    assert "pkgs: The set according to the nixpkgs.pkgs option." in text


def test_a_role_and_ordinary_inline_code_render_alike() -> None:
    """A role means "this is a name", and so does inline code."""
    assert _text("Use {var}`pkgs` here.").strip() == _text("Use `pkgs` here.").strip()


def test_a_code_fence_keeps_its_left_bar() -> None:
    """`NixCodeBlock` draws a bar, and a plain fence in a description is Nix."""
    text = _text("Text.\n\n```\n{ pkgs, ... }: { }\n```\n")
    assert "│ { pkgs, ... }: { }" in text


def test_a_colon_fence_shows_the_text_inside_it() -> None:
    """MyST writes a note as a colon fence, and CommonMark has no such thing."""
    text = _text("::: {.note}\nRestart it after a change.\n:::\n")
    assert "Restart it after a change." in text


def test_the_width_bounds_the_result() -> None:
    long_line = "word " * 60
    for width in (40, 100):
        longest = max(len(line) for line in _text(long_line, width).splitlines())
        assert longest <= width


def test_the_renderer_reads_the_terminal_when_it_is_given_no_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REPL prints into the whole terminal and passes no width.

    **The terminal is narrower than `MEASURE` here, and that is the point.**
    The measure would otherwise decide the width, and the test would pass
    whether the terminal was read or not.

    It compared a no-width render against a render at a written-down width
    until now. That passed only because the old rule gave both the same
    number: a short string put each of them at the floor of 60.
    """
    narrow = MEASURE - 30

    def _size(fallback: tuple[int, int] = (80, 24)) -> os.terminal_size:
        del fallback  # the point of the patch is that the fallback is not used
        return os.terminal_size((narrow, 24))

    monkeypatch.setattr(markdown_module.shutil, "get_terminal_size", _size)
    drawn = "".join(fragment[1] for fragment in to_formatted_text(render_markdown("plain text")))
    assert max(len(line) for line in drawn.splitlines()) == narrow


def test_nix_markdown_turns_terminal_hyperlinks_off_by_default() -> None:
    """A caller that builds the class directly gets the same behavior."""
    assert NixMarkdown("x").hyperlinks is False


#: A paragraph as nixpkgs writes one: soft-wrapped in the `.nix` source by a
#: formatter, with no hard break anywhere. `nixpkgs.pkgs` is the real option
#: this is copied from, and its longest source line is 66 characters.
_SOFT_WRAPPED = """If set, the pkgs argument to all NixOS modules is the value of
this option, extended with `nixpkgs.overlays`, if
that is also set. Either `nixpkgs.crossSystem` or
`nixpkgs.localSystem` will be used in an assertion
to check that the NixOS and Nixpkgs architectures match."""


def test_a_soft_wrapped_paragraph_reflows_to_the_pane() -> None:
    """The line breaks of the `.nix` source must not survive into the pane.

    Regression test. The render width was `longest source line + 4`, so a
    paragraph wrapped at 66 columns by a formatter was drawn at 70 columns in
    a pane of 160, which put every break back where the source had it. The
    text looked hard-wrapped, and nothing in it is.

    Measured on the real `nixpkgs.pkgs`: 24 lines before and 21 after, in a
    160-column pane.
    """
    drawn = _text(_SOFT_WRAPPED, 160)
    lines = [line.rstrip() for line in drawn.splitlines() if line.strip()]
    source = [line.rstrip() for line in _SOFT_WRAPPED.splitlines()]

    assert len(lines) < len(source), f"the paragraph kept its source shape: {lines}"
    # The first source line ends mid-sentence, so a render that reproduced the
    # source would end its first line there too.
    assert not lines[0].endswith("the value of"), "the first source break survived"


def test_a_paragraph_is_never_wider_than_the_measure() -> None:
    """A paragraph drawn across a whole wide terminal is hard to follow."""
    for pane in (100, 160, 200, 400):
        drawn = _text(_SOFT_WRAPPED, pane)
        widest = max(len(line.rstrip()) for line in drawn.splitlines())
        assert widest <= min(MEASURE, pane), f"pane {pane} drew a line of {widest}"


def test_a_pane_narrower_than_the_measure_still_bounds_the_text() -> None:
    """The measure is a ceiling, and the pane is the other one."""
    for pane in (40, 60, 80):
        drawn = _text(_SOFT_WRAPPED, pane)
        widest = max(len(line.rstrip()) for line in drawn.splitlines())
        assert widest <= pane, f"pane {pane} drew a line of {widest}"
