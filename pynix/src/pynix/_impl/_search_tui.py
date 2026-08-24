"""A full-screen search over a list of records that is already in memory.

**This layer knows nothing about Nix.** It draws a search bar across the top,
a ranked list under it and the detail of the selected record under that. The
caller gives it the records, the function that ranks them and the two
functions that draw one. `pynix search` is the first caller, over the NixOS
options in its cached index. Issue #85 adds `pynix search` over packages, and
that command wants the same interface over different records.

**The screen stacks, and each pane is the full width.** A side-by-side split
put the divider where the content asked for it, so the divider moved every
time the selection moved. Read `_PANE` for the measurement and for the rule
that holds the divider still.

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
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import HorizontalLine

from nanopynix._typechecking import BEARTYPING
from pynix._impl._columns import GUTTER, Grid, lay_out

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Mapping, Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.input import Input
    from prompt_toolkit.key_binding import KeyPressEvent
    from prompt_toolkit.output import Output

#: How tall the list is, against the detail pane. The list is the half a
#: person scans, so it takes the larger share. Measured over the 24 941
#: options of one NixOS configuration: the detail of an option is 9 to 10
#: lines, because a description is 2.4 lines on average and an option
#: declares one file. So 45 of 36 usable rows holds the whole detail of a
#: typical option with no scrolling.
_LIST_WEIGHT = 55
_DETAIL_WEIGHT = 45

#: **Both panes state a preferred size, and the number 0 is the point.**
#: `prompt_toolkit` divides a split in two passes. The first pass grows each
#: child by its weight but stops that child at its *preferred* size, and the
#: preferred size of a `Window` comes from the content when the caller states
#: none. So the divider landed wherever the selected record happened to ask
#: for. Measured on a 200-column terminal, over a side-by-side split: the
#: list was 94 columns wide for `services.openssh.enable` and 56 columns wide
#: for the next row down, a 38-column jump for one keypress.
#:
#: A stated preferred size of 0 makes the first pass do nothing, so the
#: second pass divides the whole space by weight alone. The ratio is then the
#: two numbers above and nothing else.
_PANE = 0

#: How many rows a page key moves, before the first render measures the window.
_FALLBACK_PAGE = 10

#: The width the detail renderer assumes, before the first render measures the
#: pane. It is the conventional width of a terminal.
_FALLBACK_WIDTH = 80

#: What the footer says the keys do. A terminal that cannot hold the first
#: form gets the second, which drops the verbs and keeps the keys.
_KEYS = "   arrows select   alt+up/down scroll   ctrl-c quit "
_KEYS_SHORT = "   arrows   alt+up/down   ctrl-c "

#: What the footer writes before a subject it had to cut, and the text it
#: cuts. The length of the whole prefix is what `_subject` does its arithmetic
#: with, so the two live here together.
_IN = " in "
_CUT = "..."

#: The shortest tail of a subject that still says something. Below this the
#: footer says nothing rather than ` in ...p`.
_MIN_TAIL = 4

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

    #: Return the one line that names *item* in the list of matches.
    row: Callable[[ItemT], str]

    #: Return the detail of *item*, drawn in the pane under the list. The
    #: second argument is the width of that pane in columns, because a
    #: renderer that wraps text needs a width.
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


def _subject(subject: str, room: int) -> str:
    """Say what the search covers, in exactly *room* columns.

    **A subject that does not fit keeps its end.** The tail is the part that
    changes between one run and the next: the attribute of a flake reference,
    or the release that the packages came from. The head is a directory that
    the reader already knows. `...` marks the cut.
    """
    text = f"{_IN}{subject}" if subject else ""
    if len(text) <= room:
        return text.ljust(room)
    keep = room - len(_IN) - len(_CUT)
    if keep < _MIN_TAIL:
        return " " * room
    return f"{_IN}{_CUT}{subject[-keep:]}"


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

    #: The grid that the list drew last. `list_fragments` writes it, and the
    #: keys read it, because how far `up` moves is the width of a row and
    #: that is only known once the list has been laid out.
    grid: Grid = field(default_factory=lambda: Grid(columns=1, widths=(), rows=0))

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
        """How many matches one page of the list holds.

        A page is a screen of rows, and a row holds `grid.columns` matches,
        so the two multiply. The list used to hold one match on each row and
        the two numbers were the same.
        """
        info = self._list_window.render_info
        rows = _FALLBACK_PAGE if info is None else max(1, info.window_height - 1)
        return rows * self.grid.columns

    # -- what the windows draw ---------------------------------------------

    def list_fragments(self) -> StyleAndTextTuples:
        """The matches, laid out across the width and then down.

        **The padding of a cell carries the cell's style.** A selected row
        draws in reverse, and a highlight that stopped at the last character
        of the name left a ragged block whose right edge moved with every
        keypress. The whole cell draws, so the block is a rectangle.
        """
        if not self.results:
            return [("class:search-tui.empty", "no match")]

        cells = [self.source.row(item) for item in self.results]
        self.grid = lay_out(cells, self.detail_width)

        fragments: StyleAndTextTuples = []
        for index, cell in enumerate(cells):
            column, _row = self.grid.position(index)
            style = "class:search-tui.row.selected" if index == self.selected else "class:search-tui.row"
            padding = self.grid.widths[column] - get_cwidth(cell)
            fragments.append((style, cell + " " * padding))
            last_of_row = column == self.grid.columns - 1
            if last_of_row or index == len(cells) - 1:
                fragments.append(("", "\n"))
            else:
                fragments.append(("", " " * GUTTER))
        return fragments

    def cursor(self) -> Point:
        """Where the selected cell is, so the window scrolls it into view."""
        if not self.results or not self.grid.widths:
            return Point(x=0, y=0)
        column, row = self.grid.position(self.selected)
        return Point(x=self.grid.left_edge(min(column, len(self.grid.widths) - 1)), y=row)

    def detail_fragments(self) -> StyleAndTextTuples:
        """The detail of the selected record, drawn under the list."""
        item = self.selection
        if item is None:
            return [("class:search-tui.empty", "No match. Change the query.")]
        return self.source.detail(item, self.detail_width)

    def footer_fragments(self) -> StyleAndTextTuples:
        """The status line, which counts the records and names the keys.

        **The keys never run off the end of the line, and the subject gives
        way to them.** The footer is one row, so a narrow terminal cut the
        right-hand side off -- and the right-hand side is where the keys
        were. Measured at 80 columns: the whole key help was gone, and the
        line ended in the middle of a store path. The keys are the only place
        this screen says what the keys do, so the count and the keys come
        first and the subject takes what is left.

        The width is the width of the detail pane, because that pane is the
        full width of the screen. It is `_FALLBACK_WIDTH` until the first
        render measures it, and `_measure_detail_pane` draws again when the
        real number arrives.
        """
        found = len(self.results)
        noun = self.source.noun if found == 1 else (self.source.plural or f"{self.source.noun}s")
        count = f" {found} {noun} of {len(self.source.items)}"
        width = self.detail_width
        whole = f"{_IN}{self.source.subject}" if self.source.subject else ""
        # Give up the verbs of the key help before giving up the subject: a
        # short key help still names all three keys, and a cut subject still
        # names the target.
        for keys in (_KEYS, _KEYS_SHORT):
            room = width - len(count) - len(keys)
            if room >= len(whole):
                return [("class:search-tui.footer", f"{count}{whole.ljust(room)}{keys}")]
        room = width - len(count) - len(_KEYS_SHORT)
        if room >= 0:
            return [("class:search-tui.footer", f"{count}{_subject(self.source.subject, room)}{_KEYS_SHORT}")]
        return [("class:search-tui.footer", count[:width].ljust(width))]

    # -- the application ----------------------------------------------------

    def _build_list_window(self) -> Window:
        return Window(
            content=FormattedTextControl(
                self.list_fragments,
                get_cursor_position=self.cursor,
            ),
            height=Dimension(weight=_LIST_WEIGHT, preferred=_PANE),
            always_hide_cursor=True,
            scroll_offsets=ScrollOffsets(top=1, bottom=1),
        )

    def _build_detail_window(self) -> Window:
        return Window(
            content=FormattedTextControl(self.detail_fragments),
            height=Dimension(weight=_DETAIL_WEIGHT, preferred=_PANE),
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
                    self._list_window,
                    HorizontalLine(),
                    self._detail_window,
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
        # left and right step one match; up and down step one row, which is
        # `grid.columns` matches, because the list reads across and then down.
        for key in ("left", "c-b"):
            keys.add(key)(self._on_previous)
        for key in ("right", "c-f"):
            keys.add(key)(self._on_next)
        for key in ("up", "c-p"):
            keys.add(key)(self._on_row_up)
        for key in ("down", "c-n"):
            keys.add(key)(self._on_row_down)
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

    def _on_row_up(self, _event: KeyPressEvent) -> None:
        self.move(-self.grid.columns)

    def _on_row_down(self, _event: KeyPressEvent) -> None:
        self.move(self.grid.columns)

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
