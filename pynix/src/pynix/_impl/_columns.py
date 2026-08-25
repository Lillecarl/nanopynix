"""Lay a list of cells out across the width, the way a paragraph flows.

**The list of matches reads across and then down.** A ranked list put one
match on each row, so a 160-column terminal drew a 30-character name and left
130 columns empty, and the reader paged through a fifth of what fitted. This
module answers where each cell goes.

**A grid is the wrong shape for this data, and issue #272 measured why.** The
rule used to be the one `ls` uses: take the largest number of columns whose
own widths still fit, and size each column to the widest cell in that column.
One cell of 86 columns therefore set the width of every row in its column, and
two such columns needed 171 of the 160 there were. So the opening screen of
`pynix search`, which holds every shape of name, drew a single column.

The rule now is the one a paragraph uses. Put each cell on the current line
while it fits, and start a new line when it does not. A long name then costs
the one line it lands on, and no other. Measured on a real index of 24 941
NixOS options, over the 500 rows an empty query returns:

===============  =======  ==============  ==============
query            width    grid            flow
===============  =======  ==============  ==============
(empty)          160      500 rows, 23%   139 rows, 83%
(empty)          200      250 rows, 37%   107 rows, 86%
`systemd`        160      500 rows, 25%   150 rows, 82%
`services.nginx` 160      64 rows, 54%    41 rows, 84%
===============  =======  ==============  ==============

The percentage is how much of the drawn area holds a name rather than
padding. Flow wins every row of that table, and it cuts no name to do it,
which is what the issue asked for and what a truncating grid could not give.

**Greedy is optimal here, and that is not an accident of the data.** The
ranking fixes the order, so the only freedom is where to break. A break taken
as late as possible can never lead to more lines than a break taken earlier,
so filling each line and moving on is the fewest lines there are.

**There is no cap on how many cells a line holds, and there used to be one.**
`MAX_COLUMNS` was 6, because past that the eye had to travel further down a
column than the paging it saved. That argument belongs to a grid, which a
reader scans downwards. Nothing lines up in a flow, so nothing is scanned
downwards, and a cap would only put back the empty right-hand margin that this
module now exists to remove.

**Every measurement is a display width and never a length in characters.**
The tag of a row is an emoji, which is one character and two columns. A
layout that counted characters would put the last cell of a line two columns
past the edge for each emoji on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from prompt_toolkit.utils import get_cwidth

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

#: How many columns separate one cell from the next on the same line.
GUTTER = 2


@dataclass(frozen=True)
class Flow:
    """Where every cell sits, once the cells have flowed across the width."""

    #: The display column each cell starts at, by index.
    offsets: tuple[int, ...]

    #: The row each cell sits on, by index.
    rows: tuple[int, ...]

    #: The indices each row holds, by row. Never empty, and never overlapping.
    lines: tuple[range, ...]

    @property
    def row_count(self) -> int:
        """How many rows the cells fill."""
        return len(self.lines)

    def position(self, index: int) -> tuple[int, int]:
        """The display column and the row that hold the cell at *index*."""
        return self.offsets[index], self.rows[index]

    def last_of_row(self, index: int) -> bool:
        """Whether the cell at *index* is the last one on its row."""
        return index == self.lines[self.rows[index]].stop - 1

    def neighbour(self, index: int, rows: int) -> int:
        """The cell *rows* rows away from *index*, at the nearest column.

        This is what an arrow key and a page key need, and an index offset is
        not: a row of a flow holds however many cells fitted on it, so "one
        row down" is not a fixed number of cells. Moving to the nearest column
        keeps the selection under the reader's eye, which is what the constant
        offset used to do when every row held the same count.

        A move past either end stops at that end, so a key held down settles
        rather than wrapping.
        """
        if not self.lines:
            return index
        target = max(0, min(len(self.lines) - 1, self.rows[index] + rows))
        wanted = self.offsets[index]
        # `min` keeps the first of equal distances, which is the cell to the
        # left. A tie means the reader is between two cells, and drifting left
        # is the direction that a repeated press converges in.
        return min(self.lines[target], key=lambda candidate: abs(self.offsets[candidate] - wanted))


def lay_out(cells: Sequence[str], width: int) -> Flow:
    """Flow *cells* across *width*, filling each line before starting the next.

    A cell is never cut and never wrapped. A cell wider than *width* takes a
    line of its own and runs past the edge, and the window scrolls it sideways
    rather than this function losing part of it.
    """
    offsets: list[int] = []
    rows: list[int] = []
    lines: list[range] = []
    start = 0
    used = 0
    for index, cell in enumerate(cells):
        measured = get_cwidth(cell)
        # The gutter belongs to the cell that follows one, so the first cell
        # of a line is measured without it and always fits.
        needed = measured if index == start else GUTTER + measured
        if index > start and used + needed > width:
            lines.append(range(start, index))
            start, used = index, 0
            needed = measured
        offsets.append(used if index == start else used + GUTTER)
        rows.append(len(lines))
        used += needed
    if cells:
        lines.append(range(start, len(cells)))
    return Flow(offsets=tuple(offsets), rows=tuple(rows), lines=tuple(lines))
