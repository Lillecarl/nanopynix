"""The full-screen interface over both indexes.

``pynix._impl.options_tui`` draws an option, ``pynix._impl.package_tui`` draws
a package, and this module is the one screen that holds both. It dispatches on
the type of the row and delegates, so neither of the two knows that the other
exists.

``pynix._search_merge`` holds the ranking, because a caller who prints a list
needs the same order and must not import ``prompt_toolkit`` to get it.

**Each row carries a tag, because the two indexes disagree by design.** A
package comes from the nixpkgs that the caller pinned, and an option comes
from the module system of the target. A person reading one list has to see
which of the two answered, and the width is fixed so that the names still line
up.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING
from pynix._impl import options_tui, package_tui
from pynix._impl._search_tui import SearchSource, SearchTui
from pynix._options import OptionRecord
from pynix._search_merge import make_merged_ranker, name as hit_name

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples

    from pynix._option_values import OptionValues
    from pynix._package_search import SearchablePackage
    from pynix._search_merge import SearchHit

#: The style classes of both detail panes. Neither namespace is one that
#: `prompt_toolkit` already owns, and
#: `test_no_style_class_collides_with_a_prompt_toolkit_default` states that.
STYLE: dict[str, str] = {**options_tui.STYLE, **package_tui.STYLE}

#: What a cell says the answer came from. Both are two columns wide, so the
#: names of a mixed list line up under each other.
#:
#: **An emoji is one character and two columns.** Every measurement of the
#: layout is a display width for that reason, and `pynix._impl._columns` says
#: so where it does the arithmetic.
TAGS = {"option": "🔩", "package": "📦"}

#: What a terminal that cannot encode an emoji gets instead. Both are one
#: column, so the names still line up.
ASCII_TAGS = {"option": "o", "package": "p"}


def tags() -> dict[str, str]:
    """The tags this terminal can draw.

    **The test is whether the stream can encode the emoji, and not a guess
    from the name of the terminal.** A stream that cannot encode it raises on
    the write, or draws a replacement character, and either one is worse than
    a letter.
    """
    encoding = getattr(sys.stdout, "encoding", None) or ""
    try:
        "".join(TAGS.values()).encode(encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return ASCII_TAGS
    return TAGS


def row(hit: SearchHit) -> str:
    """One cell of the list of matches, tagged with the index it came from."""
    drawn = tags()
    tag = drawn["option"] if isinstance(hit, OptionRecord) else drawn["package"]
    return f"{tag} {hit_name(hit)}"


def detail(hit: SearchHit, width: int, values: OptionValues | None = None) -> StyleAndTextTuples:
    """Draw one row in the detail pane, whichever index it came from.

    *values* reaches the option half alone. A package carries its own fields
    in the index and needs no evaluator.
    """
    if isinstance(hit, OptionRecord):
        return options_tui.detail(hit, width, values)
    return package_tui.detail(hit, width)


def source(
    options: Sequence[OptionRecord],
    packages: Sequence[SearchablePackage],
    *,
    subject: str,
    values: OptionValues | None = None,
) -> SearchSource[SearchHit]:
    """Describe both indexes to the generic interface."""

    def draw(hit: SearchHit, width: int) -> StyleAndTextTuples:
        return detail(hit, width, values)

    return SearchSource(
        items=[*options, *packages],
        rank=make_merged_ranker(options, packages),
        row=row,
        detail=draw,
        noun="match",
        plural="matches",
        subject=subject,
        style=STYLE,
        background=None if values is None else values.serve,
    )


async def browse(
    options: Sequence[OptionRecord],
    packages: Sequence[SearchablePackage],
    *,
    subject: str,
    initial_query: str = "",
    values: OptionValues | None = None,
) -> None:
    """Open the full-screen interface over both indexes."""
    await SearchTui(source(options, packages, subject=subject, values=values), initial_query=initial_query).run()
