"""Render Nix documentation Markdown into terminal text.

NixOS option descriptions and Nix `builtins` documentation are MyST Markdown,
not plain CommonMark: they use colon fences and definition lists, and their
code blocks hold Nix with no language tag. Rich renders none of those
correctly on its own, so this module subclasses the Rich Markdown elements
that get it wrong.

The REPL prints this with `print_formatted_text`, and the `search` TUI puts
it in the detail pane. Both are heavy readers, and neither is a subcommand
module: this module imports `rich` and `myst_parser`, so only a module under
`pynix._impl` may import it. `pynix._impl` says why that rule exists.
"""

from __future__ import annotations

import shutil
from io import StringIO
from typing import TYPE_CHECKING, Any, ClassVar

from markdown_it.renderer import RendererHTML
from myst_parser.config.main import MdParserConfig
from myst_parser.parsers.mdit import create_md_parser
from prompt_toolkit.formatted_text import ANSI
from rich.console import Console, ConsoleOptions, JustifyMethod, RenderResult
from rich.containers import Renderables
from rich.markdown import (
    CodeBlock,
    Heading,
    Markdown,
    MarkdownContext,
    MarkdownElement,
    TextElement,
)
from rich.segment import Segment
from rich.syntax import Syntax

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from markdown_it.token import Token


class _LeftHeading(Heading):
    """A heading that stays aligned to the left margin."""

    LEVEL_ALIGN: ClassVar[dict[str, JustifyMethod]] = {
        f"h{i}": "left"
        for i in range(1, 7)  # type: ignore[misc] -- rich TypedDict JustifyMethod is Literal
    }


class _NixCodeBlock(CodeBlock):
    """A code block with syntax highlighting and a left border bar."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code = str(self.text).rstrip()
        lexer = "nix" if self.lexer_name in ("text", "") else self.lexer_name
        syntax = Syntax(
            code,
            lexer,
            theme="ansi_dark",
            background_color="default",
            word_wrap=True,
            line_numbers=False,
        )
        bar_style = console.get_style("dim", default="none")
        bar_prefix = Segment("  │ ", bar_style)
        new_line = Segment("\n")
        render_options = options.update(width=max(options.max_width - 4, 20))
        lines = console.render_lines(syntax, render_options)
        for line in lines:
            yield bar_prefix
            yield from line
            yield new_line


class _DefList(MarkdownElement):
    new_line = True


class _DefTerm(TextElement):
    new_line = True
    style_name = "bold"

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self.text


class _DefDesc(MarkdownElement):
    new_line = True

    def __init__(self) -> None:
        self.elements: Renderables = Renderables()
        super().__init__()

    def on_child_close(self, context: MarkdownContext, child: MarkdownElement) -> bool:
        del context
        self.elements.append(child)
        return False

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        render_options = options.update(width=max(options.max_width - 4, 20))
        lines = console.render_lines(self.elements, render_options)
        prefix = Segment("  : ")
        indent = Segment("    ")
        new_line = Segment("\n")
        first = True
        for line in lines:
            yield prefix if first else indent
            yield from line
            yield new_line
            first = False


class _ColonFence(MarkdownElement):
    """Render MyST colon fence container (:::{...} ... :::)."""

    new_line = True

    @classmethod
    def create(cls, markdown: Markdown, token: Token) -> _ColonFence:  # noqa: ARG003 -- MarkdownElement protocol requires the parameter name markdown
        return cls(token.content)

    def __init__(self, content: str) -> None:
        self.content = content
        super().__init__()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if self.content:
            yield NixMarkdown(self.content)


def _renderable_inline(children: list[Token]) -> list[Token]:
    """Rewrite the inline tokens that Rich cannot draw.

    Rich knows CommonMark. MyST adds tokens on top of it, and Rich drops a
    token it does not know rather than showing the text inside it. Two of them
    reach a NixOS option description, and this pass rewrites both.
    """
    result: list[Token] = []
    index = 0
    while index < len(children):
        token = children[index]
        if _is_self_link(children, index):
            result.append(children[index + 1])
            index += 3
            continue
        result.append(_role_as_code(token))
        index += 1
    return result


def _is_self_link(children: list[Token], index: int) -> bool:
    """Say whether the link at *index* has its own address as its text.

    `hyperlinks=False` makes Rich print the address after the text. An
    autolink, which nixpkgs writes as `<https://example.com>`, already has the
    address as its text, so Rich prints the address twice. One option of
    home-manager carries an 88-character URL, and the repeat cost four lines of
    the detail pane rather than two.
    """
    token = children[index]
    return (
        token.type == "link_open"
        and index + 2 < len(children)
        and children[index + 1].type == "text"
        and children[index + 2].type == "link_close"
        and children[index + 1].content == token.attrs.get("href")
    )


def _role_as_code(token: Token) -> Token:
    """Turn a MyST role into inline code, and leave every other token alone.

    nixpkgs writes a cross reference as a role: ``{option}`nixpkgs.pkgs```,
    ``{var}`pkgs```, ``{file}`/etc/passwd```. Rich has no element for
    `myst_role`, so it drew none of them: the description of `_module.args`
    read "• : The nixpkgs package set", with the name of the option missing.

    There is nothing to link to in a terminal, and the role means "this is a
    name and not prose", which is what inline code means as well.
    """
    if token.type != "myst_role":
        return token
    # All three fields, and not the type alone: Rich reads `tag` to decide that
    # a node is inline code, and a role carries an empty one.
    token.type = "code_inline"
    token.tag = "code"
    token.markup = "`"
    return token


class NixMarkdown(Markdown):
    """Markdown customized for Nix and MyST documentation."""

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "heading_open": _LeftHeading,
        "fence": _NixCodeBlock,
        "code_block": _NixCodeBlock,
        "colon_fence": _ColonFence,
        "dl_open": _DefList,
        "dt_open": _DefTerm,
        "dd_open": _DefDesc,
    }

    def __init__(self, markup: str, **kwargs: Any) -> None:
        # **`hyperlinks=False`, and this is not a preference.** With it on,
        # Rich wraps the link text in an OSC 8 escape. `prompt_toolkit.ANSI`
        # reads CSI escapes and not OSC ones, so it drops the leading escape
        # byte and prints the rest of the sequence as text: a description that
        # named a URL showed `8;id=16117648;https://...` on the screen. With it
        # off, Rich prints the address after the text, which a terminal can
        # read and which wraps to the width like any other text.
        kwargs.setdefault("hyperlinks", False)
        super().__init__(markup, **kwargs)
        config = MdParserConfig(
            enable_extensions={
                "colon_fence",
                "deflist",
                "strikethrough",
                "tasklist",
            }
        )
        parser = create_md_parser(config, RendererHTML)
        tokens = parser.parse(markup)
        for t in tokens:
            if t.type == "colon_fence":
                t.tag = ""
            if t.type == "inline" and t.children:
                t.children = _renderable_inline(t.children)
        self.parsed = tokens


def render_markdown(text: str, width: int | None = None) -> ANSI:
    """Render Markdown into formatted ANSI text bounded by the longest line.

    *width* is how many columns the result may use. The REPL prints into the
    whole terminal and gives no width, so the terminal decides. The `search`
    interface draws into one pane of a split screen, and gives the width of
    that pane.
    """
    lines = text.splitlines()
    max_line = max((len(line.rstrip()) for line in lines), default=80)
    if width is None:
        width = shutil.get_terminal_size(fallback=(80, 24)).columns
    render_width = min(max(max_line + 4, 60), width)

    output = StringIO()
    console = Console(file=output, force_terminal=True, width=render_width)
    console.print(NixMarkdown(text))
    return ANSI(output.getvalue().rstrip("\n"))
