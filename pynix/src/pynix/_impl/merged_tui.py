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

from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING
from pynix._impl import options_tui, package_tui
from pynix._impl._search_tui import SearchSource, SearchTui
from pynix._options import OptionRecord
from pynix._search_merge import make_merged_ranker, name as hit_name

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples

    from pynix._package_search import SearchablePackage
    from pynix._search_merge import SearchHit

#: The style classes of both detail panes. Neither namespace is one that
#: `prompt_toolkit` already owns, and
#: `test_no_style_class_collides_with_a_prompt_toolkit_default` states that.
STYLE: dict[str, str] = {**options_tui.STYLE, **package_tui.STYLE}

#: What a row says the answer came from. The two are the same width, so the
#: names of a mixed list line up under each other.
TAGS = {"option": "opt", "package": "pkg"}


def row(hit: SearchHit) -> str:
    """One line of the list of matches, tagged with the index it came from."""
    tag = TAGS["option"] if isinstance(hit, OptionRecord) else TAGS["package"]
    return f"{tag}  {hit_name(hit)}"


def detail(hit: SearchHit, width: int) -> StyleAndTextTuples:
    """Draw one row in the detail pane, whichever index it came from."""
    if isinstance(hit, OptionRecord):
        return options_tui.detail(hit, width)
    return package_tui.detail(hit, width)


def source(
    options: Sequence[OptionRecord],
    packages: Sequence[SearchablePackage],
    *,
    subject: str,
) -> SearchSource[SearchHit]:
    """Describe both indexes to the generic interface."""
    return SearchSource(
        items=[*options, *packages],
        rank=make_merged_ranker(options, packages),
        row=row,
        detail=detail,
        noun="match",
        plural="matches",
        subject=subject,
        style=STYLE,
    )


async def browse(
    options: Sequence[OptionRecord],
    packages: Sequence[SearchablePackage],
    *,
    subject: str,
    initial_query: str = "",
) -> None:
    """Open the full-screen interface over both indexes."""
    await SearchTui(source(options, packages, subject=subject), initial_query=initial_query).run()
