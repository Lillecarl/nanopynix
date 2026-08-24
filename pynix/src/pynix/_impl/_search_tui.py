"""A full-screen search over a list of records that is already in memory.

**This layer knows nothing about Nix.** It draws a search bar across the top,
a ranked list on the left and the detail of the selected record on the right.
The caller gives it the records, the function that ranks them and the two
functions that draw one. `pynix search` is the first caller, over the NixOS
options in its cached index. Issue #85 adds `pynix search` over packages, and
that command wants the same interface over different records.

**Every keystroke re-ranks the records already in memory.** The interface
evaluates nothing and reads no file, so it answers a keypress in the time that
the ranking function takes.

The module imports `prompt_toolkit`, which the REPL measured at 91.8 ms, so it
lives under `pynix._impl` and no subcommand module may import it. Read
`pynix._impl` for the mechanism that keeps it off the fast path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, ScrollOffsets, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style, merge_styles
from prompt_toolkit.widgets import HorizontalLine, VerticalLine

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Mapping, Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.input import Input
    from prompt_toolkit.key_binding import KeyPressEvent
    from prompt_toolkit.output import Output

#: How wide the list is, against the detail pane. A NixOS option name reaches
#: 60 characters, and a rendered description wants the rest.
_LIST_WEIGHT = 42
_DETAIL_WEIGHT = 58

#: How many rows a page key moves, before the first render measures the window.
_FALLBACK_PAGE = 10

#: The width the detail renderer assumes, before the first render measures the
#: pane. It is the conventional width of a terminal.
_FALLBACK_WIDTH = 80

#: **The namespace is `search-tui` and not `search`, and it must stay that
#: way.** `prompt_toolkit` matches a dotted class name against each of its
#: prefixes, and its own default style defines `search` as
#: `bg:ansibrightyellow ansiblack` for an incremental search. A class called
#: `search.row` therefore inherits that, and the whole list draws black on
#: bright yellow.
STYLE_RULES: dict[str, str] = {
    "search-tui.label": "bold",
    "search-tui.row": "",
    "search-tui.row.selected": "reverse bold",
    "search-tui.empty": "italic",
    "search-tui.footer": "reverse",
}

STYLE = Style.from_dict(STYLE_RULES)


@dataclass(frozen=True)
class SearchSource[ItemT]:
    """Everything the search interface needs to know about one kind of record.

    `pynix search` fills this in with NixOS options, and issue #85 fills it in
    with packages. The interface itself reads no field of a record.
    """

    #: Every record the index holds. The interface reads the length of this, to
    #: report how large the search space is. `rank` does the searching.
    items: Sequence[ItemT]

    #: Return the records that match *query*, best first. The interface calls
    #: this on every keystroke, and calls it with an empty string when the
    #: interface opens.
    rank: Callable[[str], Sequence[ItemT]]

    #: Return the one line that names *item* in the list on the left.
    row: Callable[[ItemT], str]

    #: Return the detail of *item*, drawn in the pane on the right. The second
    #: argument is the width of that pane in columns, because a renderer that
    #: wraps text needs a width.
    #:
    #: The return type is the concrete `StyleAndTextTuples`, and not the
    #: `AnyFormattedText` union that `prompt_toolkit` accepts. That union holds
    #: a forward reference to a name in another module, and beartype cannot
    #: resolve it from here. A caller that has ANSI text passes it through
    #: `prompt_toolkit.formatted_text.to_formatted_text` first.
    detail: Callable[[ItemT, int], StyleAndTextTuples]

    #: What the footer calls one record, in the singular. The footer adds an
    #: "s" for a count that is not one.
    noun: str = "match"

    #: What the footer says for more than one, when adding an `s` is wrong.
    #: `match` gives `matchs` without it.
    plural: str = ""

    #: What the footer says the search covers, for example a flake reference.
    subject: str = ""

    #: Style classes that `detail` and `row` use, over the base ones above.
    #: A caller that styles nothing of its own leaves this empty.
    style: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass
class SearchTui[ItemT]:
    """The state of one full-screen search, and the application that draws it.

    The state is public and plain, so a test drives the real application over a
    pipe and then reads what the keys did.
    """

    source: SearchSource[ItemT]

    #: What the search bar holds when the interface opens. `search --tui
    #: <query>` puts the query of the command line here.
    initial_query: str = ""

    #: Where the application reads its keys. `None` gives the real terminal,
    #: and a test gives a pipe.
    input: Input | None = None

    #: Where the application draws. `None` gives the real terminal.
    output: Output | None = None

    #: The records that match the current query, best first. `__post_init__`
    #: fills it in, so it takes no argument and needs no default.
    results: list[ItemT] = field(init=False)

    #: Which of `results` the list has selected, as an index.
    selected: int = 0

    #: The first line of the detail pane that is on the screen.
    detail_scroll: int = 0

    #: How wide the detail pane was at the last render, in columns.
    detail_width: int = _FALLBACK_WIDTH

    def __post_init__(self) -> None:
        self.buffer = Buffer(
            multiline=False,
            document=Document(self.initial_query, len(self.initial_query)),
            on_text_changed=self._on_query_changed,
        )
        self.results = list(self.source.rank(self.initial_query))
        self._list_window = self._build_list_window()
        self._detail_window = self._build_detail_window()
        self.application = self._build_application()

    # -- state -------------------------------------------------------------

    @property
    def query(self) -> str:
        """The text in the search bar."""
        return self.buffer.text

    @property
    def selection(self) -> ItemT | None:
        """The selected record, or `None` when nothing matches."""
        if not self.results:
            return None
        return self.results[self.selected]

    def _on_query_changed(self, _buffer: Buffer) -> None:
        """Re-rank the records, and put the selection on the best match."""
        self.results = list(self.source.rank(self.buffer.text))
        self.selected = 0
        self.detail_scroll = 0

    def move(self, delta: int) -> None:
        """Move the selection by *delta* rows, and stop at each end."""
        if not self.results:
            self.selected = 0
            return
        self.selected = max(0, min(len(self.results) - 1, self.selected + delta))
        self.detail_scroll = 0

    def scroll_detail(self, delta: int) -> None:
        """Scroll the detail pane by *delta* lines, and never above the top."""
        self.detail_scroll = max(0, self.detail_scroll + delta)

    def page(self) -> int:
        """How many rows one page of the list holds."""
        info = self._list_window.render_info
        if info is None:
            return _FALLBACK_PAGE
        return max(1, info.window_height - 1)

    # -- what the windows draw ---------------------------------------------

    def list_fragments(self) -> StyleAndTextTuples:
        """The list on the left, one row for each match."""
        if not self.results:
            return [("class:search-tui.empty", "no match")]
        fragments: StyleAndTextTuples = []
        for index, item in enumerate(self.results):
            style = "class:search-tui.row.selected" if index == self.selected else "class:search-tui.row"
            fragments.append((style, self.source.row(item)))
            fragments.append(("", "\n"))
        return fragments

    def detail_fragments(self) -> StyleAndTextTuples:
        """The detail of the selected record, drawn on the right."""
        item = self.selection
        if item is None:
            return [("class:search-tui.empty", "No match. Change the query.")]
        return self.source.detail(item, self.detail_width)

    def footer_fragments(self) -> StyleAndTextTuples:
        """The status line, which counts the records and names the keys."""
        found = len(self.results)
        noun = self.source.noun if found == 1 else (self.source.plural or f"{self.source.noun}s")
        left = f" {found} {noun} of {len(self.source.items)}"
        if self.source.subject:
            left = f"{left} in {self.source.subject}"
        return [
            ("class:search-tui.footer", left),
            ("class:search-tui.footer", "   up/down select   alt+up/down scroll   ctrl-c quit "),
        ]

    # -- the application ----------------------------------------------------

    def _build_list_window(self) -> Window:
        return Window(
            content=FormattedTextControl(
                self.list_fragments,
                get_cursor_position=lambda: Point(x=0, y=self.selected),
            ),
            width=Dimension(weight=_LIST_WEIGHT),
            always_hide_cursor=True,
            scroll_offsets=ScrollOffsets(top=1, bottom=1),
        )

    def _build_detail_window(self) -> Window:
        return Window(
            content=FormattedTextControl(self.detail_fragments),
            width=Dimension(weight=_DETAIL_WEIGHT),
            wrap_lines=True,
            always_hide_cursor=True,
            get_vertical_scroll=lambda _window: self.detail_scroll,
        )

    def _build_application(self) -> Application[None]:
        layout = Layout(
            HSplit(
                [
                    VSplit(
                        [
                            Window(
                                content=FormattedTextControl([("class:search-tui.label", " search ")]),
                                width=8,
                                height=1,
                            ),
                            Window(content=BufferControl(self.buffer), height=1),
                        ]
                    ),
                    HorizontalLine(),
                    VSplit([self._list_window, VerticalLine(), self._detail_window]),
                    Window(content=FormattedTextControl(self.footer_fragments), height=1),
                ]
            ),
            focused_element=self.buffer,
        )
        return Application(
            layout=layout,
            key_bindings=self._key_bindings(),
            style=merge_styles([STYLE, Style.from_dict(dict(self.source.style))]),
            full_screen=True,
            mouse_support=True,
            after_render=self._measure_detail_pane,
            input=self.input,
            output=self.output,
        )

    def _measure_detail_pane(self, application: Application[None]) -> None:
        """Record how wide the detail pane is, for a renderer that wraps text.

        **This runs after a render, and draws again when the width changed.**
        A window knows its width only once it has been rendered, so the first
        render has to use `_FALLBACK_WIDTH` -- and before this hook drew again,
        nothing ever corrected it: the opening screen wrapped its text to 80
        columns in a pane of 83, and the window then broke a word in half.
        Measured with a probe that drew its own width: 80 on the first render
        and 83 on every one after a keypress.

        The second render settles it, because the width then matches and this
        asks for no third. A terminal that is resized takes the same path.
        """
        info = self._detail_window.render_info
        if info is not None and info.window_width != self.detail_width:
            self.detail_width = info.window_width
            application.invalidate()

    def _key_bindings(self) -> KeyBindings:
        """Bind each key to the method that answers it.

        The bindings are named methods, and not the closures that the
        `prompt_toolkit` examples use. Two things follow: a test calls one
        directly, and pyright can see that each one is used.
        """
        keys = KeyBindings()
        for key in ("up", "c-p"):
            keys.add(key)(self._on_previous)
        for key in ("down", "c-n"):
            keys.add(key)(self._on_next)
        keys.add("pageup")(self._on_page_up)
        keys.add("pagedown")(self._on_page_down)
        keys.add("escape", "up")(self._on_scroll_up)
        keys.add("escape", "down")(self._on_scroll_down)
        for key in ("c-c", "c-d", "c-q", "escape"):
            keys.add(key)(self._on_quit)
        keys.add("enter")(self._on_select)
        return keys

    def _on_previous(self, _event: KeyPressEvent) -> None:
        self.move(-1)

    def _on_next(self, _event: KeyPressEvent) -> None:
        self.move(1)

    def _on_page_up(self, _event: KeyPressEvent) -> None:
        self.move(-self.page())

    def _on_page_down(self, _event: KeyPressEvent) -> None:
        self.move(self.page())

    def _on_scroll_up(self, _event: KeyPressEvent) -> None:
        self.scroll_detail(-1)

    def _on_scroll_down(self, _event: KeyPressEvent) -> None:
        self.scroll_detail(1)

    def _on_quit(self, event: KeyPressEvent) -> None:
        event.app.exit()

    def _on_select(self, _event: KeyPressEvent) -> None:
        """Answer `enter` with nothing, on purpose.

        The binding is what stops the buffer from accepting the line and
        closing the interface. Issue #238 leaves the eventual action out of
        scope: printing the configuration of the selected record has to answer
        what an `attrsOf` option means first, and that question needs an issue
        of its own.
        """
        return

    async def run(self) -> None:
        """Draw the interface, and return when the caller leaves it.

        **There is no synchronous entry point, on purpose.**
        `Application.run` calls `asyncio.run`, and every `pynix` command
        already runs inside an event loop, so that call raises "asyncio.run()
        cannot be called from a running event loop".
        """
        await self.application.run_async()
