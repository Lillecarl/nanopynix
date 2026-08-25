"""Tests for the Markdown renderer that the REPL and `search` share.

A NixOS option description is MyST Markdown, and Rich knows CommonMark. Each
test here pins one place where the two disagree, and each one comes from a real
description rather than from an invented string.
"""

from __future__ import annotations

import os

import pytest
from prompt_toolkit.formatted_text import FormattedText, StyleAndTextTuples
from prompt_toolkit.styles import Attrs, Style

import pynix._markdown as markdown_module
from pynix._markdown import MEASURE, NixMarkdown, render_markdown


def _attrs(style_str: str) -> Attrs:
    """What `prompt_toolkit` reads out of one style string."""
    return Style([("probe", style_str)]).get_attrs_for_style_str("class:probe")


def _text(markup: str, width: int = 78) -> str:
    """The visible text that the renderer produces, with the styles removed."""
    return "".join(fragment[1] for fragment in render_markdown(markup, width))


def _styled(markup: str, wanted: str, width: int = 78) -> str:
    """The style string of the span that holds *wanted*."""
    fragments: StyleAndTextTuples = render_markdown(markup, width)
    for style, text, *_rest in fragments:
        if wanted in text:
            return style
    message = f"no span holds {wanted!r}: {fragments}"
    raise AssertionError(message)


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


#: One sample of each construction that a NixOS option description uses.
_EVERY_CONSTRUCTION = (
    "Use **bold**, *it*, `code` and {var}`pkgs` here.",
    "Text.\n\n```\n{ pkgs, ... }: { a = 1; }\n```\n",
    "See <https://example.com/a/b> and [the docs](https://example.com/c).",
    "::: {.note}\nRestart it after a change.\n:::\n",
    "term\n: the description\n\nother\n: more\n",
    "- one\n- two\n\n1. first\n2. second\n",
    "# Title\n\n## Sub\n\nbody\n",
    "~~gone~~ and normal\n",
    "> a quote\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
)


@pytest.mark.parametrize("markup", _EVERY_CONSTRUCTION)
def test_no_escape_byte_reaches_the_fragments(markup: str) -> None:
    """The path from Markdown to fragments writes no escape byte at all.

    Regression test, and it states the whole path rather than one flag. Rich
    encoded each style into ANSI escapes and `prompt_toolkit.ANSI` parsed them
    back one step later, so a style that Rich could write and that parser
    could not read was lost: a link became an OSC 8 escape, and the option
    `programs.vscode.enterprisePolicies` showed `8;id=16117648;https://...` as
    text. Issue #255 removed the two steps in the middle.
    """
    for style, text, *_rest in render_markdown(markup, 78):
        assert "\x1b" not in text, f"an escape byte reached the text: {text!r}"
        assert "\x1b" not in style, f"an escape byte reached the style: {style!r}"


def test_a_bold_span_carries_the_bold_attribute() -> None:
    assert _attrs(_styled("Use **bold** here.", "bold")).bold is True


def test_an_italic_span_carries_the_italic_attribute() -> None:
    assert _attrs(_styled("Use *slanted* here.", "slanted")).italic is True


def test_inline_code_carries_its_colour() -> None:
    """Rich draws inline code in cyan on black, and the colour must survive."""
    attributes = _attrs(_styled("Use `code` here.", "code"))
    assert attributes.color == "ansicyan"
    assert attributes.bgcolor == "ansiblack"


def test_a_code_block_keeps_its_syntax_highlighting() -> None:
    """The Nix lexer colours a name and a number differently.

    A code block reaches the fragments through `Syntax`, which is a second
    renderer inside the first one. This states that its styles survive too.
    """
    markup = "```\n{ a = 1; }\n```\n"
    name = _attrs(_styled(markup, "a"))
    number = _attrs(_styled(markup, "1"))
    assert name.color is not None
    assert number.color is not None
    assert name.color != number.color


def test_the_result_is_formatted_text_and_not_a_bare_list() -> None:
    """`print_formatted_text` reads the two apart, and the REPL uses it.

    Regression test. It takes a bare list as a sequence of objects to print,
    so `:doc builtins.map` printed the repr of the fragments as one line of
    text. `to_formatted_text` names the same reason in its own comment, and a
    test that compares the drawn text against the old renderer cannot see it.
    """
    assert isinstance(render_markdown("plain text"), FormattedText)


def test_a_link_is_readable_and_carries_no_escape() -> None:
    """A terminal cannot follow a link, so the address has to be text."""
    fragments = render_markdown("See [the docs](https://example.com/a/b) now.", 78)
    text = "".join(fragment[1] for fragment in fragments)
    assert "the docs" in text
    assert "https://example.com/a/b" in text
    assert all("\x1b" not in fragment[1] for fragment in fragments)


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
    drawn = "".join(fragment[1] for fragment in render_markdown("plain text"))
    assert max(len(line) for line in drawn.splitlines()) == narrow


def test_nix_markdown_turns_terminal_hyperlinks_off_by_default() -> None:
    """With hyperlinks on, Rich draws the text and hides the address.

    It puts the address in the `link` field of the style, which nothing here
    reads. `test_a_named_link_keeps_its_address` states the effect, and this
    states the mechanism for a caller that builds the class itself.
    """
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
