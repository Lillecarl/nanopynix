"""The ranking that every `pynix` search shares.

`search` searches NixOS options and issue #85 searches packages. The two hold
different records and ask the same question, so the algorithm is here and not
in either of them.

**A match holds every word of the query, and a score only puts the matches in
order.** A fuzzy score alone cannot answer this. `partial_ratio` finds the best
window of the text that looks like the query, and a short query has many such
windows: over a real index of 14 752 options, `vsc` gave 27 matches and `vsco`
gave 500, because a four-letter query matches three of its four letters almost
everywhere. A caller who types one more letter must get fewer results and never
more.

So a search runs in two stages:

1. Keep the records whose haystack holds every word of the query, ignoring
   case. Two words narrow, where one long word cannot: `vscode enable` gives
   2 options where `vscode` gives 23.
2. Order those by `WRatio` against the name, then by length, then by name.
   `WRatio` scales a score down when the two strings differ greatly in length,
   which is wrong for a filter and right for an order: it puts
   `programs.vscode.enable` above a 60-character name holding the same word.

**The haystack and the name are separate on purpose.** A package matches on its
attribute, its description and the binaries it installs, and none of those
belong in the text that decides the order -- a long description would push
every package that has one to the bottom. So the filter reads the haystack and
the order reads the name.

**A query that no haystack holds falls back to a fuzzy search**, bounded by
`CUTOFF`. A caller who types `vscodee` has made a typo, and an empty screen
answers nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from rapidfuzz import fuzz, process

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Sequence

#: The record that a search ranks.
#:
#: **A `TypeVar`, and not the PEP 695 `def make_ranker[ItemT]` syntax.** This
#: module uses future annotations, so every annotation is a string that
#: beartype resolves against the globals of the module at the first call. A
#: type parameter of a *function* is not one of those globals, and beartype
#: raises `BeartypeCallHintForwardRefException: Forward reference "ItemT"
#: unimportable from module "pynix._ranking"`. A module-level name resolves.
#: The PEP 695 syntax on a *class* is fine, and `pynix._impl._search_tui` uses
#: it: a class carries its parameters where beartype looks.
ItemT = TypeVar("ItemT")

#: How many records a search returns. A list scrolls, so this is not the height
#: of a window. It is the point past which a better match is not worth the time
#: that `rapidfuzz` spends to find it.
RESULTS = 500

#: The score below which a near match does not answer, out of 100. It bounds
#: the fallback alone, and never the ordinary search.
CUTOFF = 70


def make_ranker(  # noqa: UP047 -- beartype cannot resolve a PEP 695 function type parameter in a stringized annotation; see `ItemT` above
    items: Sequence[ItemT],
    *,
    name: Callable[[ItemT], str],
    haystack: Callable[[ItemT], str] | None = None,
    limit: int = RESULTS,
) -> Callable[[str], Sequence[ItemT]]:
    """Build the function that a search calls on every keystroke.

    *name* gives the text that decides the order, and that names a record in a
    list. *haystack* gives the text that a query must be found in; it defaults
    to *name*, which is what a search over names alone wants.

    The returned function closes over the text of every record, lowercased
    once, so a keystroke costs the search and nothing else. Measured over
    24 941 options: 10 ms for an ordinary query, and 47 ms for a single letter,
    which every record holds and which therefore scores the whole limit.
    """
    reader = haystack if haystack is not None else name
    # Sorted by name, so that records which score alike come out in a stable
    # and predictable order.
    ordered = sorted(items, key=name)
    names = [name(item) for item in ordered]
    haystacks = [reader(item).lower() for item in ordered]

    def rank(query: str) -> Sequence[ItemT]:
        # An empty query has nothing to hold, and every record scores alike.
        # Give the caller the records by name instead.
        if not query:
            return ordered[:limit]
        words = query.lower().split()
        held = [index for index, text in enumerate(haystacks) if all(word in text for word in words)]
        if held:
            scored = process.extract(
                query,
                {index: names[index] for index in held},
                scorer=fuzz.WRatio,
                processor=str.lower,
                limit=limit,
            )
            best = sorted(scored, key=lambda match: (-match[1], len(match[0]), match[0]))
            return [ordered[index] for _text, _score, index in best]
        near = process.extract(
            query,
            dict(enumerate(names)),
            scorer=fuzz.WRatio,
            processor=str.lower,
            score_cutoff=CUTOFF,
            limit=limit,
        )
        return [ordered[index] for _text, _score, index in near]

    return rank
