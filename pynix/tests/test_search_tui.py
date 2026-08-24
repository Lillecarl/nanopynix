"""Tests for the generic full-screen search interface.

Each test drives the real `prompt_toolkit` application over a pipe, and then
reads the state that the keys left behind. There is no double for the
application: a binding that never fires, or a key that the parser reads as a
different key, is exactly the defect that these tests must catch.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles.defaults import PROMPT_TOOLKIT_STYLE, WIDGETS_STYLE

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

    The rule reaches a `SearchSource` that carries a style of its own as well.
    This test reads the classes of this module, because they are the ones that
    every source inherits.
    """
    reserved = {name for name, _value in PROMPT_TOOLKIT_STYLE}
    reserved |= {name for name, _value in WIDGETS_STYLE}
    for class_name in STYLE_RULES:
        head = class_name.split(".")[0]
        assert head not in reserved, f"class:{class_name} inherits the default style of {head!r}"
