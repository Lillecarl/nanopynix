"""Render Nix documentation Markdown into terminal text.

NixOS option descriptions and Nix `builtins` documentation are MyST Markdown,
not plain CommonMark: they use colon fences and definition lists, and their
code blocks hold Nix with no language tag. Rich renders none of those
correctly on its own, so this module subclasses the Rich Markdown elements
that get it wrong.

The REPL prints this with `print_formatted_text`, and the `osearch` TUI puts
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
        self.parsed = tokens


def render_markdown(text: str) -> ANSI:
    """Render Markdown into formatted ANSI text bounded by the longest line."""
    lines = text.splitlines()
    max_line = max((len(line.rstrip()) for line in lines), default=80)
    terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
    render_width = min(max(max_line + 4, 60), terminal_width)

    output = StringIO()
    console = Console(file=output, force_terminal=True, width=render_width)
    console.print(NixMarkdown(text))
    return ANSI(output.getvalue().rstrip("\n"))
