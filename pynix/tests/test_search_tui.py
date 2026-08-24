"""Tests for the generic full-screen search interface.

Each test drives the real `prompt_toolkit` application over a pipe, and then
reads the state that the keys left behind. There is no double for the
application: a binding that never fires, or a key that the parser reads as a
different key, is exactly the defect that these tests must catch.
"""

from __future__ import annotations

from dataclasses import dataclass

import anyio
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles.defaults import PROMPT_TOOLKIT_STYLE, WIDGETS_STYLE

from pynix._impl import options_tui, package_tui
from pynix._impl._search_tui import STYLE_RULES, SearchSource, SearchTui

#: The escape sequences that a terminal sends for the keys under test. Each one
#: goes through the real key parser of `prompt_toolkit`.
_DOWN = "\x1b[B"
_UP = "\x1b[A"
_ALT_DOWN = "\x1b\x1b[B"
_ALT_UP = "\x1b\x1b[A"
_ENTER = "\r"
_CTRL_C = "\x03"
_CTRL_D = "\x04"
_CTRL_Q = "\x11"


@dataclass(frozen=True)
class _Fruit:
    name: str
    colour: str


_FRUIT = (
    _Fruit("apple", "green"),
    _Fruit("apricot", "orange"),
    _Fruit("banana", "yellow"),
    _Fruit("blackberry", "black"),
    _Fruit("cherry", "red"),
)


def _source() -> SearchSource[_Fruit]:
    """A source whose ranking is a plain substring filter, so it is predictable."""
    return SearchSource(
        items=_FRUIT,
        rank=lambda query: [fruit for fruit in _FRUIT if query in fruit.name],
        row=lambda fruit: fruit.name,
        detail=lambda fruit, width: [("", f"{fruit.name} is {fruit.colour} ({width})")],
        noun="fruit",
        subject="the fruit bowl",
    )


async def _drive(keys: str) -> SearchTui[_Fruit]:
    """Feed *keys* to the real application, and return it once it exits.

    The caller ends *keys* with a key that leaves the interface. Without one
    the application would wait for a keypress that never arrives, and the test
    would hang rather than fail.
    """
    with create_pipe_input() as pipe:
        tui = SearchTui(_source(), input=pipe, output=DummyOutput())
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            await tui.application.run_async()
        return tui


def _names(tui: SearchTui[_Fruit]) -> list[str]:
    return [fruit.name for fruit in tui.results]


#: The screen that `_Sized` reports. `DummyOutput` reports 80 columns, which is
#: `_FALLBACK_WIDTH` exactly, so a width test needs a different number to say
#: anything.
_COLUMNS = 132
_ROWS = 40


class _Sized(DummyOutput):
    """A `DummyOutput` that reports a screen, so a render divides a real size.

    `DummyOutput.get_size` reports 80 by 24. A test that measures a pane needs
    to know what it is measuring against.
    """

    def get_size(self) -> Size:
        return Size(rows=_ROWS, columns=_COLUMNS)


def test_the_interface_opens_on_every_record() -> None:
    """An empty query ranks every record, and selects the first."""
    tui = SearchTui(_source())
    assert _names(tui) == [fruit.name for fruit in _FRUIT]
    assert tui.selected == 0
    assert tui.selection is not None
    assert tui.selection.name == "apple"


async def test_typing_re_ranks_the_records() -> None:
    tui = await _drive(f"berry{_CTRL_C}")
    assert tui.query == "berry"
    assert _names(tui) == ["blackberry"]


async def test_the_arrow_keys_move_the_selection() -> None:
    tui = await _drive(f"{_DOWN}{_DOWN}{_CTRL_C}")
    assert tui.selected == 2
    assert tui.selection is not None
    assert tui.selection.name == "banana"

    tui = await _drive(f"{_DOWN}{_UP}{_CTRL_C}")
    assert tui.selected == 0


async def test_the_selection_stops_at_each_end() -> None:
    tui = await _drive(f"{_UP}{_UP}{_CTRL_C}")
    assert tui.selected == 0

    tui = await _drive(_DOWN * 20 + _CTRL_C)
    assert tui.selected == len(_FRUIT) - 1


async def test_a_new_query_puts_the_selection_back_on_the_best_match() -> None:
    """Typing after a move must not leave the selection past the new results."""
    tui = await _drive(f"{_DOWN * 4}cherry{_CTRL_C}")
    assert tui.selected == 0
    assert _names(tui) == ["cherry"]


async def test_alt_arrow_scrolls_the_detail_pane() -> None:
    tui = await _drive(f"{_ALT_DOWN}{_ALT_DOWN}{_CTRL_C}")
    assert tui.detail_scroll == 2


async def test_the_detail_pane_never_scrolls_above_its_top() -> None:
    tui = await _drive(f"{_ALT_DOWN}{_ALT_UP * 5}{_CTRL_C}")
    assert tui.detail_scroll == 0


async def test_moving_the_selection_returns_the_detail_pane_to_the_top() -> None:
    tui = await _drive(f"{_ALT_DOWN}{_ALT_DOWN}{_DOWN}{_CTRL_C}")
    assert tui.selected == 1
    assert tui.detail_scroll == 0


async def test_enter_does_not_leave_the_interface() -> None:
    """`enter` is a deliberate no-op, and must not accept the buffer and exit."""
    tui = await _drive(f"app{_ENTER}ricot{_CTRL_C}")
    assert tui.query == "appricot"
    assert tui.results == []


@pytest.mark.parametrize("key", [_CTRL_C, _CTRL_D, _CTRL_Q])
async def test_each_quit_key_leaves_the_interface(key: str) -> None:
    """Ctrl-C, Ctrl-D and Ctrl-Q each end the application.

    `_drive` returns only when the application exits, so a key that failed to
    quit would hang this test rather than fail an assertion.
    """
    tui = await _drive(key)
    assert tui.query == ""


async def test_no_match_leaves_no_selection() -> None:
    tui = await _drive(f"zzz{_CTRL_C}")
    assert tui.results == []
    assert tui.selection is None
    assert tui.list_fragments() == [("class:search-tui.empty", "no match")]
    assert tui.detail_fragments() == [("class:search-tui.empty", "No match. Change the query.")]


def test_the_footer_counts_the_matches_and_names_the_subject() -> None:
    tui = SearchTui(_source())
    text = "".join(fragment[1] for fragment in tui.footer_fragments())
    assert "5 fruits of 5" in text
    assert "the fruit bowl" in text

    tui.results = [_FRUIT[0]]
    text = "".join(fragment[1] for fragment in tui.footer_fragments())
    assert "1 fruit of 5" in text


def test_the_list_marks_the_selected_row() -> None:
    tui = SearchTui(_source())
    tui.selected = 2
    styles = [fragment[0] for fragment in tui.list_fragments() if fragment[1] != "\n"]
    assert styles[2] == "class:search-tui.row.selected"
    assert styles[0] == "class:search-tui.row"


def test_the_detail_pane_gets_its_measured_width() -> None:
    """The renderer needs a width, and starts at 80 until the first render."""
    tui = SearchTui(_source())
    assert tui.detail_width == 80
    tui.detail_width = 132
    assert tui.detail_fragments() == [("", "apple is green (132)")]


@pytest.mark.anyio
async def test_the_divider_stays_put_when_the_selection_moves() -> None:
    """Moving the selection must not move the divider between the two panes.

    Regression test. `prompt_toolkit` divides a split in two passes, and the
    first pass stops each child at its *preferred* size. A `Window` that
    states no preferred size takes the one its content asks for, so the
    divider landed wherever the selected record happened to reach.

    Measured on a 200-column terminal, over the side-by-side split this
    replaced: the list was 94 columns for `services.openssh.enable` and 56
    columns for the row under it, a 38-column jump for one keypress. Stacking
    the panes alone did not answer it -- the same probe then gave 20, 17 and
    18 rows for three consecutive records, because the jump had only moved to
    the other axis. `_PANE` is the answer, and this test is what states it.

    The three records below differ in how much detail they draw, which is the
    input the defect needed.
    """
    records = [
        _Fruit("apple", "green"),
        _Fruit("fig", "purple"),
        _Fruit("blackcurrant", "black"),
    ]
    source = SearchSource(
        items=records,
        rank=lambda _query: records,
        row=lambda fruit: fruit.name,
        # One line for each character of the name, so each record asks the
        # pane for a different height and a different width.
        detail=lambda fruit, _width: [("", "\n".join(fruit.name * n for n in range(1, len(fruit.name))))],
        noun="fruit",
    )

    heights: list[int] = []
    with create_pipe_input() as pipe:
        tui = SearchTui(source, input=pipe, output=_Sized())
        with create_app_session(input=pipe, output=_Sized()):

            async def walk() -> None:
                for index in range(len(records)):
                    tui.selected = index
                    tui.application.invalidate()
                    await anyio.sleep(0.05)
                    info = tui._list_window.render_info
                    if info is not None:
                        heights.append(info.window_height)
                tui.application.exit()

            async with anyio.create_task_group() as group:
                group.start_soon(tui.application.run_async)
                group.start_soon(walk)

    assert len(heights) == len(records), "the list pane did not draw for every record"
    assert len(set(heights)) == 1, f"the divider moved between records: {heights}"


def test_a_page_key_falls_back_before_the_first_render() -> None:
    tui = SearchTui(_source())
    assert tui.page() == 10


def test_no_style_class_collides_with_a_prompt_toolkit_default() -> None:
    """A class name must not begin with one that `prompt_toolkit` defines.

    Regression test. `prompt_toolkit` matches a dotted class name against each
    of its prefixes, and its default style defines `search` as
    `bg:ansibrightyellow ansiblack` for an incremental search. The classes here
    were `search.row` and `search.footer`, so the whole list drew black on
    bright yellow in a real terminal. No headless test saw it: a style reaches
    the screen through the renderer, and `DummyOutput` renders nothing.

    **Every style dictionary of the program is here, and not this one alone.**
    The first version of this test read `STYLE_RULES` only, because those are
    the classes each source inherits. A `SearchSource` carries classes of its
    own, and issue #257 renamed `osearch` to `search` across the tree, which
    turned `osearch.name` into `search.name` in the detail pane of an option
    and reintroduced the same defect one layer down. One test over every
    dictionary is what catches that.
    """
    reserved = {name for name, _value in PROMPT_TOOLKIT_STYLE}
    reserved |= {name for name, _value in WIDGETS_STYLE}
    dictionaries = {
        "pynix._impl._search_tui.STYLE_RULES": STYLE_RULES,
        "pynix._impl.options_tui.STYLE": options_tui.STYLE,
        "pynix._impl.package_tui.STYLE": package_tui.STYLE,
    }
    for where, rules in dictionaries.items():
        for class_name in rules:
            head = class_name.split(".")[0]
            assert head not in reserved, f"{where}: class:{class_name} inherits the default style of {head!r}"


async def test_the_detail_pane_is_drawn_at_its_real_width() -> None:
    """The opening screen must not draw at the fallback width.

    Regression test. A window knows its width only once it has been rendered,
    so the first render uses `_FALLBACK_WIDTH`. Nothing then drew again, so the
    opening screen wrapped its text to 80 columns inside a pane of 83 and the
    window broke a word in half. Measured with a probe that drew its own width:
    80 on the first render, and 83 only after a keypress.

    The application now measures after a render and draws once more when the
    width changed, so the width the pane really has reaches `detail` with no
    input at all.

    **The terminal is 132 columns wide, and that number is what makes this a
    test.** The detail pane is the full width of the screen, so a terminal of
    80 columns gives a pane of 80 -- the fallback width exactly, and an
    assertion against it then passes whether the measurement runs or not.
    """
    widths: list[int] = []
    source = SearchSource(
        items=_FRUIT,
        rank=lambda _query: _FRUIT,
        row=lambda fruit: fruit.name,
        detail=lambda _fruit, width: [("", str(widths.append(width) or width))],
        noun="fruit",
    )
    with create_pipe_input() as pipe:
        tui = SearchTui(source, input=pipe, output=_Sized())
        with create_app_session(input=pipe, output=_Sized()):
            # **The quit key waits for a render, and this is not a style
            # choice.** Queued before the application starts, it is read in
            # the same pass as the first render, so the application leaves
            # before the redraw that `_measure_detail_pane` asked for. The
            # test then reads the fallback width and fails, whether the
            # measurement works or not.
            async def quit_once_drawn() -> None:
                await anyio.sleep(0.05)
                pipe.send_text(_CTRL_C)

            async with anyio.create_task_group() as group:
                group.start_soon(tui.application.run_async)
                group.start_soon(quit_once_drawn)

    assert widths, "the detail pane never drew"
    assert tui.detail_width != 80, "the pane kept the fallback width"
    assert tui.detail_width == _COLUMNS, "the pane is the full width of the screen"
    assert widths[-1] == tui.detail_width
    # The second render is what settles it, and no third is asked for.
    assert widths.count(tui.detail_width) >= 1
