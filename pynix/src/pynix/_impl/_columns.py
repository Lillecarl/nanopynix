"""Lay a list of cells out in columns that fill the width first.

**The list of matches reads across and then down.** A ranked list put one
match on each row, so a 160-column terminal drew a 30-character name and left
130 columns empty, and the reader paged through a fifth of what fitted. This
module answers how many columns the width holds, and how wide each one is.

The rule is the one `ls` uses: take the largest number of columns whose own
widths still fit, and size each column to the widest cell in that column. A
column count that does not fit is not offered, so nothing is ever cut.

**Every measurement is a display width and never a length in characters.**
The tag of a row is an emoji, which is one character and two columns. A
layout that counted characters would put the right-hand column two columns
past the edge for each emoji on the row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from prompt_toolkit.utils import get_cwidth

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

#: How many columns separate one column of cells from the next.
GUTTER = 2

#: The most columns to offer. A search result is read, not scanned like a
#: directory listing, and past this the eye has to travel further than the
#: paging it saves.
MAX_COLUMNS = 6


@dataclass(frozen=True)
class Grid:
    """How many columns the cells take, how wide each is, and how many rows."""

    #: How many cells one row holds. At least 1, so the arithmetic of a
    #: caller that divides by it is always safe.
    columns: int

    #: The display width of each column, left to right.
    widths: tuple[int, ...]

    #: How many rows the cells fill.
    rows: int

    def position(self, index: int) -> tuple[int, int]:
        """The column and the row that hold the cell at *index*."""
        return index % self.columns, index // self.columns

    def left_edge(self, column: int) -> int:
        """The display column that *column* starts at."""
        return sum(self.widths[:column]) + GUTTER * column


def _column_widths(measured: Sequence[int], columns: int) -> list[int]:
    """The width of each column, when the cells read across and then down.

    Reading across means the cells of one column are every `columns`-th one,
    so the step of the range is what states the reading order.
    """
    return [max(measured[column::columns], default=0) for column in range(columns)]


def lay_out(cells: Sequence[str], width: int, *, max_columns: int = MAX_COLUMNS) -> Grid:
    """Fit *cells* into *width* columns of the screen, across and then down.

    A cell is never cut and never wrapped. When even one column of the widest
    cell does not fit, the answer is still one column, and the window scrolls
    that cell sideways rather than this function losing part of it.
    """
    if not cells:
        return Grid(columns=1, widths=(width,), rows=0)

    measured = [get_cwidth(cell) for cell in cells]
    one = Grid(columns=1, widths=(max(measured),), rows=len(cells))
    ceiling = min(len(cells), max_columns, max(1, (width + GUTTER) // (min(measured) + GUTTER)))

    for columns in range(ceiling, 1, -1):
        rows = -(-len(cells) // columns)
        widths = _column_widths(measured, columns)
        if sum(widths) + GUTTER * (columns - 1) <= width:
            return Grid(columns=columns, widths=tuple(widths), rows=rows)
    return one
