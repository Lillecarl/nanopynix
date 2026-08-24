"""The full-screen interface of ``pynix osearch``.

``pynix._impl.osearch`` decides which mode to run, and it reaches this module
through the PEP 562 table of ``pynix._impl``. That attribute read is what
imports ``prompt_toolkit`` and the Markdown renderer, so a caller who gave a
query on the command line pays for neither. Measured: this module adds 115
``prompt_toolkit`` modules and 69 Markdown ones.

``pynix._impl._search_tui`` draws the screen, and knows nothing about Nix. This
module is the half that knows what a NixOS option is: how to rank one, and how
to draw one in the detail pane.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import to_formatted_text
from rapidfuzz import fuzz, process

from nanopynix._typechecking import BEARTYPING
from pynix._impl._search_tui import SearchSource, SearchTui
from pynix._markdown import render_markdown

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples

    from pynix._options import OptionRecord

#: How many options the interface ranks. The list scrolls, so this is not the
#: height of the window. It is the point past which a better match is not worth
#: the time that `rapidfuzz` spends to find it, and a real
#: `nixosConfigurations.<host>` exposes 10 000 to 20 000 options.
_RESULTS = 500

#: The score below which a near match does not reach the list, out of 100.
#: It bounds the fallback alone, and never the ordinary substring search.
_CUTOFF = 70

#: The style classes that the detail pane uses, over the ones that
#: `pynix._impl._search_tui` defines for the screen itself.
STYLE: dict[str, str] = {
    "osearch.name": "bold",
    "osearch.type": "ansicyan",
    "osearch.flag": "ansiyellow",
    "osearch.label": "bold",
    "osearch.path": "ansigreen",
}


def rank(records: Sequence[OptionRecord]) -> Callable[[str], Sequence[OptionRecord]]:
    """Return the function that the interface calls on every keystroke.

    **A match holds every word of the query, and a score only puts the matches
    in order.** A fuzzy score alone cannot answer this question. `partial_ratio`
    finds the best window of the name that looks like the query, and a short
    query has many such windows: over a real index of 14 752 options, `vsc`
    gave 27 matches and `vsco` gave 500, because a four-letter query matches
    three of its four letters almost everywhere. A caller who types one more
    letter must get fewer options and not more.

    So the search has two stages:

    1. Keep the names that hold every word of the query, and ignore case. Two
       words narrow: `vscode enable` gives 2 options where `vscode` gives 23.
    2. Put those names in order by `WRatio`, then by length, then by name.
       `WRatio` scales a score down when the two strings differ in length,
       which is wrong for a filter and right for an order: it puts
       `programs.vscode.enable` above a name of 60 characters that holds the
       same word.

    **A query that no name holds falls back to a fuzzy search.** A caller who
    types `vscodee` has made a typo, and an empty screen answers nothing.
    `_CUTOFF` bounds that fallback, and bounds nothing else.

    The function closes over the names and their lowercase forms, built once,
    so a keystroke costs the search and nothing else. Measured on that same
    index: 6 ms for an ordinary query, and 26 ms for a single letter, which
    matches every option and so scores the whole cap.
    """
    by_name = {record.name: record for record in records}
    # Sorted, so that the options which score alike come out in a stable and
    # predictable order.
    names = sorted(by_name)
    lowered = {name: name.lower() for name in names}
    ordered = [by_name[name] for name in names]

    def rank_query(query: str) -> Sequence[OptionRecord]:
        # An empty query has nothing to hold, and every name scores alike.
        # Give the caller the options by name instead.
        if not query:
            return ordered[:_RESULTS]
        words = query.lower().split()
        held = [name for name in names if all(word in lowered[name] for word in words)]
        if held:
            scored = process.extract(query, held, scorer=fuzz.WRatio, processor=str.lower, limit=_RESULTS)
            best = sorted(scored, key=lambda match: (-match[1], len(match[0]), match[0]))
            return [by_name[name] for name, _score, _index in best]
        near = process.extract(
            query,
            names,
            scorer=fuzz.WRatio,
            processor=str.lower,
            score_cutoff=_CUTOFF,
            limit=_RESULTS,
        )
        return [by_name[name] for name, _score, _index in near]

    return rank_query


def detail(record: OptionRecord, width: int) -> StyleAndTextTuples:
    """Draw one option in the pane on the right.

    A description is MyST Markdown, and `render_markdown` is the renderer that
    the REPL uses for the same text. It takes the width of the pane, because
    the pane is one half of a split screen and not the whole terminal.
    """
    fragments: StyleAndTextTuples = [
        ("class:osearch.name", record.name),
        ("", "\n"),
        ("class:osearch.type", record.type),
        ("", "\n"),
    ]
    if record.read_only:
        fragments += [("class:osearch.flag", "read only"), ("", "\n")]
    if record.description:
        fragments.append(("", "\n"))
        fragments += to_formatted_text(render_markdown(record.description, width))
        fragments.append(("", "\n"))
    if record.declarations:
        fragments += [("", "\n"), ("class:osearch.label", "declared in"), ("", "\n")]
        for path in record.declarations:
            fragments += [("class:osearch.path", f"  {path}"), ("", "\n")]
    return fragments


def source(records: Sequence[OptionRecord], subject: str) -> SearchSource[OptionRecord]:
    """Describe the options to the generic interface."""
    return SearchSource(
        items=records,
        rank=rank(records),
        row=lambda record: record.name,
        detail=detail,
        noun="option",
        subject=subject,
        style=STYLE,
    )


async def browse(records: Sequence[OptionRecord], *, subject: str, initial_query: str = "") -> None:
    """Open the full-screen interface over *records*.

    *subject* is what the footer says the search covers, and *initial_query* is
    what the search bar holds when the interface opens.
    """
    await SearchTui(source(records, subject), initial_query=initial_query).run()
