"""Tests for the generic full-screen search interface.

Each test drives the real `prompt_toolkit` application over a pipe, and then
reads the state that the keys left behind. There is no double for the
application: a binding that never fires, or a key that the parser reads as a
different key, is exactly the defect that these tests must catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles.defaults import PROMPT_TOOLKIT_STYLE, WIDGETS_STYLE

from pynix._impl import options_tui, package_tui
from pynix._impl._search_tui import _CUT, _IN, _KEYS_SHORT, _MIN_TAIL, STYLE_RULES, SearchSource, SearchTui

if TYPE_CHECKING:
    from collections.abc import Sequence

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
_LEFT = "\x1b[D"
_RIGHT = "\x1b[C"


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


#: How many lines the tall source draws, which is more than any pane holds.
_TALL = 200


def _tall_source() -> SearchSource[_Fruit]:
    """A source whose detail is taller than the pane, so a scroll can move it.

    The plain source draws one line, and a window that already shows every
    line has nowhere to scroll to. A test of the scroll needs text under the
    fold, which is the shape of a real option: `nixpkgs.pkgs` has 18 lines of
    description.
    """
    plain = _source()
    lines = "\n".join(f"line {number}" for number in range(_TALL))
    return SearchSource(
        items=plain.items,
        rank=plain.rank,
        row=plain.row,
        detail=lambda _fruit, _width: [("", lines)],
        noun=plain.noun,
        subject=plain.subject,
    )


#: How long a drive waits between one write to the input and the next, in
#: seconds. It is a wait for a render, and a render of this fixture is
#: microseconds.
_SETTLE = 0.05


async def _run(source: SearchSource[_Fruit] | None, writes: Sequence[str]) -> SearchTui[_Fruit]:
    """Start the application, write each of *writes* to it, and return it.

    **Every write happens while the application is running, and that is not a
    detail of style.** `prompt_toolkit` attaches the read end of the input to
    the event loop after its first render, so a key written before the start
    is read in the same pass as that render. Two things follow, and this
    module met both:

    - The application leaves before the redraw that the keys asked for, so a
      test that reads what the window *drew* reads the opening screen every
      time. Issue #270 is the defect that hid behind it.
    - It is the shape that hangs in CI. `test-local-nix_2_35` and
      `test-local-git` lose every test of this module to the 120-second
      deadline, and neither reproduces in the dev shell. A key that is
      already in the pipe when the reader attaches is the one difference
      between this harness and a person at a terminal. Issue #271.
    """
    with create_pipe_input() as pipe:
        tui = SearchTui(source or _source(), input=pipe, output=DummyOutput())
        with create_app_session(input=pipe, output=DummyOutput()):

            async def write() -> None:
                for text in writes:
                    await anyio.sleep(_SETTLE)
                    pipe.send_text(text)

            async with anyio.create_task_group() as group:
                group.start_soon(tui.application.run_async)
                group.start_soon(write)
        return tui


async def _drive_drawn(keys: str, source: SearchSource[_Fruit] | None = None) -> SearchTui[_Fruit]:
    """Feed *keys*, let the application draw, and only then leave it.

    The quit key is a write of its own, so a render happens between the keys
    and the exit. `_run` says why that matters.
    """
    return await _run(source, [keys, _CTRL_C])


async def _drive(keys: str, source: SearchSource[_Fruit] | None = None) -> SearchTui[_Fruit]:
    """Feed *keys* to the real application, and return it once it exits.

    The caller ends *keys* with a key that leaves the interface. Without one
    the application would wait for a keypress that never arrives, and the test
    would hang rather than fail.
    """
    return await _run(source, [keys])


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
    """Left and right step one match, because the list reads across."""
    tui = await _drive(f"{_RIGHT}{_RIGHT}{_CTRL_C}")
    assert tui.selected == 2
    assert tui.selection is not None
    assert tui.selection.name == "banana"

    tui = await _drive(f"{_RIGHT}{_LEFT}{_CTRL_C}")
    assert tui.selected == 0


async def test_up_and_down_move_by_a_whole_row() -> None:
    """A row holds `grid.columns` matches, so that is what `down` steps.

    The list held one match on each row until issue #265, and `down` stepped
    one match. It now reads across and then down, so a step of one match is
    `right` and a step of one row is `down`.
    """
    tui = await _drive(f"{_DOWN}{_CTRL_C}")
    assert tui.grid.columns >= 1
    assert tui.selected == min(tui.grid.columns, len(_FRUIT) - 1)

    back = await _drive(f"{_DOWN}{_UP}{_CTRL_C}")
    assert back.selected == 0


async def test_the_selection_stops_at_each_end() -> None:
    tui = await _drive(f"{_UP}{_UP}{_CTRL_C}")
    assert tui.selected == 0

    tui = await _drive(_RIGHT * 20 + _CTRL_C)
    assert tui.selected == len(_FRUIT) - 1


async def test_a_new_query_puts_the_selection_back_on_the_best_match() -> None:
    """Typing after a move must not leave the selection past the new results."""
    tui = await _drive(f"{_RIGHT * 4}cherry{_CTRL_C}")
    assert tui.selected == 0
    assert _names(tui) == ["cherry"]


async def test_alt_arrow_scrolls_the_detail_pane() -> None:
    tui = await _drive_drawn(f"{_ALT_DOWN}{_ALT_DOWN}", _tall_source())
    assert tui.detail_scroll == 2


async def test_the_detail_pane_never_scrolls_above_its_top() -> None:
    tui = await _drive_drawn(f"{_ALT_DOWN}{_ALT_UP * 5}", _tall_source())
    assert tui.detail_scroll == 0


async def test_alt_down_moves_what_the_detail_window_draws() -> None:
    """The window has to move, and not only the number that counts the keys.

    Regression test for issue #270. `wrap_lines=True` makes `prompt_toolkit`
    pick the scroll function that never reads `get_vertical_scroll`, so the
    hook this screen passed was dead code and the pane stood still. Every
    test above reads `detail_scroll`, which was right the whole time.
    """
    tui = await _drive_drawn(f"{_ALT_DOWN * 3}", _tall_source())
    assert tui.detail_scroll == 3
    assert tui.detail_top == 3


async def test_the_end_of_a_long_detail_is_reachable() -> None:
    """A scroll that stops early hides the end of the text.

    `default` and `example` are drawn under the description, so an option
    with a long description hides its own default when this fails.
    """
    tui = await _drive_drawn(f"{_ALT_DOWN * _TALL}", _tall_source())
    assert tui.detail_top > 0
    assert tui.detail_scroll == tui.detail_top


async def test_a_detail_that_fits_does_not_scroll() -> None:
    """A key held down must not leave the count below the last line.

    The next `alt+up` would then move nothing, once for each press over the
    end. `_measure_detail_pane` cuts the request back to what the window did.
    """
    tui = await _drive_drawn(f"{_ALT_DOWN * 5}")
    assert tui.detail_scroll == 0
    assert tui.detail_top == 0


async def test_moving_the_selection_returns_the_detail_pane_to_the_top() -> None:
    """The subject is the scroll, so the move is the one-match step.

    `down` steps a whole row, and a row of this fixture holds every match, so
    a `down` here lands on the last one. `right` states the same thing about
    the scroll and says what it means.
    """
    tui = await _drive_drawn(f"{_ALT_DOWN}{_ALT_DOWN}{_RIGHT}", _tall_source())
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


def test_the_list_marks_the_selected_cell() -> None:
    """One cell carries the selected style, and it is the selected one.

    The list emits a gutter and a newline between cells, so this reads the
    styled cells alone. A test that read every fragment would count the
    separators as cells and land on the wrong one.
    """
    tui = SearchTui(_source())
    tui.selected = 2
    cells = [fragment[0] for fragment in tui.list_fragments() if fragment[0].startswith("class:search-tui.row")]
    assert len(cells) == len(_FRUIT)
    assert cells[2] == "class:search-tui.row.selected"
    assert cells.count("class:search-tui.row.selected") == 1
    assert cells[0] == "class:search-tui.row"


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


_SUBJECT = "/home/lillecarl/Code/croshome#nixosConfigurations.hetztop"


def _footer_at(width: int) -> str:
    """The whole footer line, as the screen would draw it at *width*."""
    source = SearchSource(
        items=_FRUIT,
        rank=lambda _query: _FRUIT,
        row=lambda fruit: fruit.name,
        detail=lambda fruit, _width: [("", fruit.name)],
        noun="fruit",
        subject=_SUBJECT,
    )
    tui = SearchTui(source)
    tui.detail_width = width
    return "".join(text for _style, text, *_rest in tui.footer_fragments())


@pytest.mark.parametrize("width", [200, 160, 120, 100, 80, 60, 40, 20])
def test_the_footer_is_exactly_as_wide_as_the_screen(width: int) -> None:
    """The footer draws a bar, so it fills the line and never overruns it."""
    assert len(_footer_at(width)) == width


@pytest.mark.parametrize("width", [200, 160, 120, 100, 80])
def test_the_footer_keeps_the_keys_before_it_keeps_the_subject(width: int) -> None:
    """The keys are the only place the screen says what the keys do.

    Regression test. The footer put the count and the subject first and the
    keys last, and a narrow terminal cut the end of the line off. Measured at
    80 columns against one NixOS configuration: the whole key help was gone,
    and the line ended in the middle of a store path.

    80 columns is the narrow end of this list because a conventional terminal
    is 80 columns, and it is where the old footer lost every key.
    """
    footer = _footer_at(width)
    for key in ("up/down", "alt+up/down", "ctrl-c"):
        assert key in footer, f"{width} columns lost {key}"


def test_a_subject_the_footer_cannot_hold_keeps_its_tail() -> None:
    """The tail is the part that changes between one run and the next."""
    footer = _footer_at(100)
    assert _SUBJECT not in footer, "the whole subject fitted, so this proves nothing"
    assert "..." in footer
    assert footer.count("nixosConfigurations.hetztop") == 1


def test_a_footer_with_no_room_for_a_subject_says_nothing_rather_than_a_stub() -> None:
    """` in ...p` is noise. Below a useful tail the subject goes.

    The width is derived and not written down, so a change to the key help
    moves the width with it rather than turning this into a test of nothing.
    It is the widest screen that still refuses the subject: the count, the
    short key help, and one column less than the shortest tail that says
    something.
    """
    count = f" {len(_FRUIT)} fruits of {len(_FRUIT)}"
    room = len(_IN) + len(_CUT) + _MIN_TAIL - 1
    footer = _footer_at(len(count) + len(_KEYS_SHORT) + room)
    assert "..." not in footer
    assert "ctrl-c" in footer


@pytest.mark.parametrize("width", [40, 80, 160])
def test_the_list_of_binaries_wraps_on_a_space_and_not_on_a_column(width: int) -> None:
    """A package that installs many programs must keep each name whole.

    Regression test. The detail pane put every binary on one line and let
    the window wrap it. The window wraps on the column, so it broke a name
    in half. Measured over `programs.sqlite` for `x86_64-linux`: `magma`
    installs 547 programs, and its one line was 10610 characters -- 67 rows
    of a 160-column terminal, with a broken name at each of the 66 wraps.
    """
    names = [f"program-{index}" for index in range(200)]
    laid_out = package_tui._names(names, width)

    for line in laid_out.split("\n"):
        assert len(line) <= width, f"a line ran to {len(line)} columns of {width}"
    for name in names:
        assert name in laid_out, f"{name} did not survive the layout"


def test_a_binary_list_that_fits_stays_on_one_line() -> None:
    """The layout must not break a short list up for no reason."""
    assert "\n" not in package_tui._names(["scp", "sftp", "ssh"], 80)


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
