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
from prompt_toolkit.formatted_text import FormattedText
from rich.color import ColorType
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
    from rich.color import Color
    from rich.style import Style


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
        # Rich puts the address in the `link` field of the style and draws the
        # text alone. Nothing here reads that field, and a terminal cannot
        # follow a link, so the address would simply disappear. With it off,
        # Rich prints the address after the text, which a reader can copy and
        # which wraps to the width like any other text.
        #
        # The flag also kept an OSC 8 escape out of the output until issue
        # #255. That reason is gone: this module builds fragments from `Style`
        # objects now, and writes no escape byte at all.
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


#: The widest a paragraph may be drawn, whatever the terminal holds.
#:
#: **This replaced a width taken from the longest line of the source, and
#: that is the whole point.** A NixOS option description is soft-wrapped in
#: the `.nix` file that declares it, by whatever formatter the author ran.
#: CommonMark reflows those lines correctly -- a single newline inside a
#: paragraph is a space and not a break -- and then a render width of
#: `longest source line + 4` put every break back where the source had it.
#:
#: Measured on `nixpkgs.pkgs`, whose description holds no hard break at all:
#: the longest source line is 66, so a 160-column pane drew the paragraph at
#: 70 columns and reproduced the source line for line. It is 21 lines here,
#: against 24, and it reads as prose.
#:
#: **The number comes from the index and not from taste.** Over the 813
#: descriptions longer than 400 characters, in a 160-column pane, against
#: the source-derived width they replaced:
#:
#: ===========  ==============  ==============================
#: this measure  total lines     descriptions still narrowed
#: ===========  ==============  ==============================
#: 80            +10.5%          4742 of 24924
#: 90            +3.0%           3054 of 24924
#: 100           -3.2%           2060 of 24924
#: 120           -12.6%          921 of 24924
#: ===========  ==============  ==============================
#:
#: 100 is where the change stops costing lines. Going wider keeps buying
#: them, and stops being a line a reader can follow: typography puts a
#: comfortable line at 45 to 90 characters, so this is already at the edge.
MEASURE = 100


#: The `prompt_toolkit` name of each of the 16 terminal colours, in the order
#: that Rich numbers them. A name lets the terminal choose the shade, and
#: `#rrggbb` does not, so a standard colour must not become a triplet.
_ANSI_NAMES = (
    "ansiblack",
    "ansired",
    "ansigreen",
    "ansiyellow",
    "ansiblue",
    "ansimagenta",
    "ansicyan",
    "ansigray",
    "ansibrightblack",
    "ansibrightred",
    "ansibrightgreen",
    "ansibrightyellow",
    "ansibrightblue",
    "ansibrightmagenta",
    "ansibrightcyan",
    "ansiwhite",
)

#: Each flag of a Rich style, with the `prompt_toolkit` word for it.
_FLAGS = (
    ("bold", "bold"),
    ("dim", "dim"),
    ("italic", "italic"),
    ("underline", "underline"),
    ("strike", "strike"),
    ("blink", "blink"),
    ("reverse", "reverse"),
    ("conceal", "hidden"),
)


def _colour(colour: Color | None) -> str:
    """Say what one Rich colour is called in a `prompt_toolkit` style string.

    A style that names no colour gives `None`, and the span inherits. A style
    that names `Color.default()` gives the DEFAULT type, which means "reset to
    the colour of the terminal" and not "inherit". `_NixCodeBlock` asks for
    that one, to stop the syntax theme painting a background of its own.
    """
    if colour is None:
        return ""
    if colour.type is ColorType.DEFAULT:
        return "ansidefault"
    number = colour.number
    if number is not None and number < len(_ANSI_NAMES):
        return _ANSI_NAMES[number]
    triplet = colour.get_truecolor()
    return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"


def _style_string(style: Style | None) -> str:
    """Turn one Rich style into a `prompt_toolkit` style string."""
    if style is None:
        return ""
    parts = [word for field, word in _FLAGS if getattr(style, field)]
    foreground = _colour(style.color)
    if foreground:
        parts.append(foreground)
    background = _colour(style.bgcolor)
    if background:
        parts.append(f"bg:{background}")
    return " ".join(parts)


def render_markdown(text: str, width: int | None = None) -> FormattedText:
    """Render Markdown into formatted text, reflowed to a readable width.

    *width* is how many columns the result may use. The REPL prints into the
    whole terminal and gives no width, so the terminal decides. The `search`
    interface draws into the detail pane of a stacked screen, and gives the
    width of that pane. Either way the text is drawn no wider than `MEASURE`.

    **The result carries no escape byte, and that is the reason this function
    reads segments rather than a string.** Rich wrote the style of each span
    into ANSI escapes, and `prompt_toolkit.ANSI` parsed them back one step
    later. A style that Rich could write and that parser could not read was
    lost: a link became an OSC 8 escape, and the screen showed
    `8;id=16117648;https://...` as text. `Console.render_lines` gives the same
    spans with a `Style` object on each one, so no escape is ever written.

    **The result is a `FormattedText` and not a plain list of tuples, and the
    REPL can tell.** `print_formatted_text` reads a bare list as a sequence of
    objects to print, so `:doc builtins.map` printed the repr of the fragments
    as one line of text. `to_formatted_text` names the same reason in its own
    comment.
    """
    if width is None:
        width = shutil.get_terminal_size(fallback=(80, 24)).columns
    render_width = min(MEASURE, width)

    console = Console(file=StringIO(), force_terminal=True, width=render_width)
    lines = console.render_lines(NixMarkdown(text), pad=False)
    # `console.print` wrote a newline after the last line, and the caller
    # stripped it. Drop an empty line here for the same reason. A line of
    # spaces is not empty: a Rich table pads its bottom row that way, and the
    # string path kept it.
    while lines and not any(segment.text for segment in lines[-1]):
        lines.pop()

    fragments = FormattedText()
    for index, line in enumerate(lines):
        if index:
            fragments.append(("", "\n"))
        fragments += [
            (_style_string(segment.style), segment.text) for segment in line if segment.text and not segment.control
        ]
    return fragments
