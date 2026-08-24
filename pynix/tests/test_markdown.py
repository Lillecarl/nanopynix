"""Tests for the Markdown renderer that the REPL and `osearch` share.

A NixOS option description is MyST Markdown, and Rich knows CommonMark. Each
test here pins one place where the two disagree, and each one comes from a real
description rather than from an invented string.
"""

from __future__ import annotations

from prompt_toolkit.formatted_text import ANSI, to_formatted_text

from pynix._markdown import NixMarkdown, render_markdown


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


def test_the_renderer_reads_the_terminal_when_it_is_given_no_width() -> None:
    """The REPL prints into the whole terminal and passes no width."""
    assert _text("plain text") == "".join(fragment[1] for fragment in to_formatted_text(render_markdown("plain text")))


def test_nix_markdown_turns_terminal_hyperlinks_off_by_default() -> None:
    """A caller that builds the class directly gets the same behavior."""
    assert NixMarkdown("x").hyperlinks is False
