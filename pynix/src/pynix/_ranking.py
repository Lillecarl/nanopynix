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

**A tier decides the order, and the score only breaks a tie inside one.**
Issue #257 measured why. `WRatio` saturates at 90 for any substring hit, so
almost every match on both sides of a search carries the same number, and the
score cannot separate them. Over the real indexes, `'ssh'` gave `autossh` 90
and `nix.sshServe.enable` 90; `'port'` gave `_90secondportraits` 90 and
`boot.initrd.luks.fido2Support` 90. Only an exact match reaches 100.

So a match falls in one of four tiers, best first:

1. :data:`EXACT` -- a key of the record equals the query;
2. :data:`ALIAS` -- an alias of the record equals the query;
3. :data:`PREFIX` -- a key or an alias starts with the query;
4. :data:`WORDS` -- every word of the query appears in the haystack;
5. :data:`FUZZY` -- the fallback above :data:`CUTOFF`, when nothing else
   matched at all.

Inside a tier the shorter name wins, then the alphabet. That rule puts
`openssh` above `opensshWithKerberos`, and both of those really do install
`ssh-keygen`.

**A key is not the name, and an alias is not a key.** *keys* gives the texts
that name the record: a package passes its attribute and its pname, and an
option passes each component of its path, so `openssh` reaches
`services.openssh.enable` through the middle component. *aliases* gives the
texts that the record answers to without being called that, which for a
package is every binary it installs.

**Two sources merge because they share the key.** A search over options and a
search over packages both return :data:`RankKey`, so one sorted list holds
both and neither source needs to know about the other.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from rapidfuzz import fuzz, process

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Mapping, Sequence

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

#: A key of the record equals the query.
EXACT = 0

#: An alias of the record equals the query.
#:
#: **A name beats an alias, and a real pair says why.** `vim` installs `vim`
#: and `xxd`, and `xxd` installs `xxd`. A person who types `xxd` means the
#: package of that name, so the alias of `vim` must rank below it. The two
#: names are the same length, so no tie-break inside one tier can decide it.
ALIAS = 1

#: A key or an alias of the record starts with the query.
PREFIX = 2

#: Every word of the query appears in the haystack.
WORDS = 3

#: Nothing above matched anywhere, and this is the near miss.
FUZZY = 4

#: The score that a tier above `WORDS` carries. Such a match needs no score:
#: the tier already ordered it, and a fixed value keeps `WRatio` from
#: reordering an exact match below a longer one.
_CERTAIN = 100.0

#: What orders one match, and smaller is better: the tier, the negated score,
#: the length of the name, and the name.
#:
#: **Two searches merge because they share this.** A search over options and a
#: search over packages both produce it, so one `sorted` call over the two
#: lists gives one order, and neither source knows about the other.
RankKey = tuple[int, float, int, str]

#: A character above every one that a key holds, so that `query + this` bounds
#: the prefix range that `bisect` finds. `\uffff` is not a legal code point in
#: a name that Nix writes, and a key that held it would only widen the range.
_AFTER_EVERY_CHARACTER = "\uffff"


@dataclass(frozen=True)
class Texts[RecordT]:
    """How a ranker reads one record.

    The four belong together: each one answers a different question about the
    same record, and a search that merges two sources holds one of these for
    each source. They were four keyword arguments until `aliases` arrived and
    made the signature six wide.

    *name* names the record in a list, and breaks a tie inside a tier.
    *keys* are the texts that name it, and default to *name* alone.
    *aliases* are the texts it answers to without being called that, and
    default to none. *haystack* is the text a word of the query must be found
    in, and defaults to *name*.
    """

    name: Callable[[RecordT], str]
    keys: Callable[[RecordT], Sequence[str]] | None = None
    aliases: Callable[[RecordT], Sequence[str]] | None = None
    haystack: Callable[[RecordT], str] | None = None


class _Keys[RecordT]:
    """The text of every record, read once, in the shapes a query needs.

    **A PEP 695 class parameter, where the functions here use a `TypeVar`.**
    beartype resolves a parameter that a class carries, and cannot resolve one
    that a function carries; `ItemT` above gives the measurement. The name
    differs from `ItemT` so that a reader does not take the two for one thing.
    """

    def __init__(
        self,
        items: Sequence[RecordT],
        name: Callable[[RecordT], str],
        keys: Callable[[RecordT], Sequence[str]],
        aliases: Callable[[RecordT], Sequence[str]],
        haystack: Callable[[RecordT], str],
    ) -> None:
        # Sorted by name, so that records which rank alike come out in a
        # stable and predictable order.
        self.ordered = sorted(items, key=name)
        self.names = [name(item) for item in self.ordered]
        self.haystacks = [haystack(item).lower() for item in self.ordered]
        self.exact: dict[str, list[int]] = {}
        self.alias: dict[str, list[int]] = {}
        # One sorted list of (text, index), so that a prefix is a slice that
        # `bisect` finds. A scan over every key would cost the whole index on
        # every keystroke: 24 571 packages carry about 75 000 keys. A prefix
        # reads an alias too, so that `ssh-key` still reaches `openssh`.
        flat: list[tuple[str, int]] = []
        for index, item in enumerate(self.ordered):
            for text in keys(item):
                lowered = text.lower()
                self.exact.setdefault(lowered, []).append(index)
                flat.append((lowered, index))
            for text in aliases(item):
                lowered = text.lower()
                self.alias.setdefault(lowered, []).append(index)
                flat.append((lowered, index))
        flat.sort()
        self.flat = flat
        self.flat_keys = [text for text, _index in flat]

    def prefixed(self, query: str) -> set[int]:
        """Every record holding a key that starts with *query*."""
        start = bisect.bisect_left(self.flat_keys, query)
        stop = bisect.bisect_left(self.flat_keys, query + _AFTER_EVERY_CHARACTER)
        return {index for _text, index in self.flat[start:stop]}

    def tiers(self, query: str) -> dict[int, int]:
        """The tier of every record that *query* matches, best tier kept."""
        words = query.split()
        found: dict[int, int] = {}
        for index in self.exact.get(query, ()):
            found[index] = EXACT
        for index in self.alias.get(query, ()):
            found.setdefault(index, ALIAS)
        for index in self.prefixed(query):
            found.setdefault(index, PREFIX)
        for index, text in enumerate(self.haystacks):
            if index not in found and all(word in text for word in words):
                found[index] = WORDS
        return found


def make_tiered_ranker(  # noqa: UP047 -- beartype cannot resolve a PEP 695 function type parameter in a stringized annotation; see `ItemT` above
    items: Sequence[ItemT],
    texts: Texts[ItemT],
    *,
    limit: int = RESULTS,
) -> Callable[[str], Sequence[tuple[RankKey, ItemT]]]:
    """Build the ranker, and give each match the key that ordered it.

    *texts* says how to read one record. The returned function closes over the
    text of every record, lowercased once, so a keystroke costs the search and
    nothing else.
    """
    name = texts.name

    def only_the_name(item: ItemT) -> Sequence[str]:
        return (name(item),)

    def nothing(item: ItemT) -> Sequence[str]:
        del item
        return ()

    read = _Keys[ItemT](
        items,
        name,
        texts.keys if texts.keys is not None else only_the_name,
        texts.aliases if texts.aliases is not None else nothing,
        texts.haystack if texts.haystack is not None else name,
    )

    def rank(query: str) -> Sequence[tuple[RankKey, ItemT]]:
        # An empty query has nothing to match, and every record ranks alike.
        # Give the caller the records by name instead.
        if not query:
            # **The length is left out of the key here, and only here.** An
            # empty query ranks every record alike, so the name alone should
            # order them. With the length in, a merged list put every short
            # package above every long option path and no option was on the
            # first screen at all.
            taken = range(min(limit, len(read.ordered)))
            return [((WORDS, -_CERTAIN, 0, read.names[i]), read.ordered[i]) for i in taken]
        lowered = query.lower().strip()
        tiers = read.tiers(lowered)
        if tiers:
            return _ordered(_scored(tiers, read.names, query), read.names, read.ordered, limit)
        near = process.extract(
            query,
            dict(enumerate(read.names)),
            scorer=fuzz.WRatio,
            processor=str.lower,
            score_cutoff=CUTOFF,
            limit=limit,
        )
        found = {index: (FUZZY, score) for _text, score, index in near}
        return _ordered(found, read.names, read.ordered, limit)

    return rank


def _scored(tiers: Mapping[int, int], names: Sequence[str], query: str) -> dict[int, tuple[int, float]]:
    """Give each match its tier and the score that breaks a tie inside it.

    A tier above `WORDS` takes `_CERTAIN`, because the tier already ordered it.
    A `WORDS` match takes its `WRatio` score, which does separate a few of them
    even though it saturates for most: measured over the real options, `rg`
    scored `age.secretsDir` at 60 where the rest scored 90.
    """
    wordy = {index: names[index] for index, tier in tiers.items() if tier == WORDS}
    scores = {
        index: score
        for _text, score, index in process.extract(
            query, wordy, scorer=fuzz.WRatio, processor=str.lower, limit=len(wordy) or 1
        )
    }
    return {index: (tier, scores.get(index, 0.0) if tier == WORDS else _CERTAIN) for index, tier in tiers.items()}


def _ordered(  # noqa: UP047 -- see make_tiered_ranker
    found: Mapping[int, tuple[int, float]],
    names: Sequence[str],
    ordered: Sequence[ItemT],
    limit: int,
) -> list[tuple[RankKey, ItemT]]:
    """The best *limit* matches, each with the key that ordered it."""
    rows = [((tier, -score, len(names[index]), names[index]), ordered[index]) for index, (tier, score) in found.items()]
    rows.sort(key=lambda row: row[0])
    return rows[:limit]


def make_ranker(  # noqa: UP047 -- see make_tiered_ranker
    items: Sequence[ItemT],
    texts: Texts[ItemT],
    *,
    limit: int = RESULTS,
) -> Callable[[str], Sequence[ItemT]]:
    """The records that *query* matches, best first, without the keys.

    A caller that searches one source alone wants this. A caller that merges
    two sources wants :func:`make_tiered_ranker`, whose keys are what put the
    two into one order.
    """
    tiered = make_tiered_ranker(items, texts, limit=limit)

    def rank(query: str) -> Sequence[ItemT]:
        return [item for _key, item in tiered(query)]

    return rank
