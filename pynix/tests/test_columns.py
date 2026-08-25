"""The flow layout of the list of matches, and the moves an arrow key makes.

Issue #272 replaced a grid with a flow, because one 86-column name set the
width of every row in its column and the opening screen of `pynix search`
drew a single column on a 160-column terminal.

The cells here are plain ASCII, so a display width is a length, except in the
one test that says otherwise.
"""

from __future__ import annotations

from pynix._impl._columns import GUTTER, lay_out


def test_no_cells_gives_no_rows() -> None:
    flow = lay_out([], 80)

    assert flow.row_count == 0
    assert flow.lines == ()


def test_cells_that_fit_take_one_row() -> None:
    flow = lay_out(["aa", "bb", "cc"], 80)

    assert flow.row_count == 1
    assert flow.lines == (range(3),)
    assert flow.offsets == (0, 2 + GUTTER, 2 * (2 + GUTTER))


def test_a_line_breaks_when_the_next_cell_would_not_fit() -> None:
    # Three cells of 4, with a gutter of 2 between them: 4, 10, 16. A width of
    # 12 holds the first two and not the third.
    flow = lay_out(["aaaa", "bbbb", "cccc"], 12)

    assert flow.lines == (range(2), range(2, 3))
    assert flow.rows == (0, 0, 1)
    assert flow.offsets == (0, 6, 0)


def test_the_gutter_belongs_to_the_cell_that_follows_one() -> None:
    """A line that fits exactly is not broken by the gutter after its last cell."""
    flow = lay_out(["aaaa", "bbbb"], 10)

    assert flow.row_count == 1


def test_one_long_cell_costs_only_its_own_row() -> None:
    """The defect of issue #272, stated as the behaviour that replaced it.

    A grid gave every one of these a row, because the long cell set the width
    of its column. The flow spends one row on the long one and packs the rest.
    """
    cells = ["x" * 8] * 4 + ["y" * 40] + ["x" * 8] * 4

    flow = lay_out(cells, 40)

    assert flow.rows[4] == 1
    assert flow.lines[1] == range(4, 5)
    assert flow.row_count == 3


def test_a_cell_wider_than_the_width_takes_a_row_and_is_not_cut() -> None:
    """The window scrolls it sideways rather than this function losing part of it."""
    flow = lay_out(["short", "w" * 100, "short"], 20)

    assert flow.lines == (range(1), range(1, 2), range(2, 3))
    assert flow.offsets[1] == 0


def test_an_emoji_is_measured_in_columns_and_not_in_characters() -> None:
    """The tag of a row is one character and two columns.

    A layout that counted characters would put the last cell of a line two
    columns past the edge for each emoji on it.
    """
    # Two cells of "emoji + space + two letters" are 4 columns each, so with
    # the gutter they need 10 and a width of 9 cannot hold both.
    cells = ["\N{GEAR} ab", "\N{GEAR} cd"]

    assert lay_out(cells, 10).row_count == 1
    assert lay_out(cells, 9).row_count == 2


def test_last_of_row_marks_where_the_line_break_goes() -> None:
    flow = lay_out(["aaaa", "bbbb", "cccc"], 12)

    assert [flow.last_of_row(index) for index in range(3)] == [False, True, True]


def test_a_row_move_lands_on_the_nearest_column() -> None:
    """What `up` and `down` need, and an index offset cannot give.

    The first row holds three cells and the second holds one, so "one row
    down" from the third cell is not a fixed number of cells away.
    """
    flow = lay_out(["aa", "bb", "cc", "d" * 12], 12)

    assert flow.lines == (range(3), range(3, 4))
    assert flow.neighbour(2, 1) == 3
    assert flow.neighbour(3, -1) == 0


def test_a_row_move_keeps_the_column_when_it_can() -> None:
    flow = lay_out(["aa", "bb", "cc", "dd", "ee", "ff"], 12)

    assert flow.lines == (range(3), range(3, 6))
    assert flow.neighbour(1, 1) == 4
    assert flow.neighbour(5, -1) == 2


def test_a_row_move_stops_at_each_end() -> None:
    """A key held down settles rather than wrapping."""
    flow = lay_out(["aa", "bb", "cc", "dd"], 8)

    assert flow.neighbour(0, -5) == 0
    assert flow.neighbour(3, 5) == 3


def test_a_row_move_over_a_single_row_stays_on_it() -> None:
    flow = lay_out(["aa", "bb"], 80)

    assert flow.neighbour(1, 1) == 1
    assert flow.neighbour(1, -1) == 1
