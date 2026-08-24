"""One order over the option index and the package index.

`pynix search` reads whichever of the two the target offers, and a person who
types one word wants one answer. So the two ranked lists become one, and each
row says which index it came from.

**The merge is a `sorted` call, and `pynix._ranking` is what makes it one.**
Both sources rank into the same `RankKey`, which holds the tier, the score,
the length of the name and the name. Neither source knows about the other, and
adding a third would need no change here beyond a name for it.

Issue #257 measured why the key cannot be the `rapidfuzz` score alone:
`WRatio` saturates at 90 for almost every substring hit, so a merged list
sorted by score is an arbitrary tie-break over hundreds of rows.

**This module imports no `prompt_toolkit`.** A caller who prints a list needs
the merge and not the screen, so the screen reads this and never the reverse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING
from pynix._option_search import tiered as tiered_options
from pynix._options import OptionRecord
from pynix._package_search import SearchablePackage, tiered as tiered_packages
from pynix._ranking import RESULTS

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Sequence

    from pynix._ranking import RankKey

#: One row of a search that reads both indexes.
#:
#: **A union, and not a wrapper.** A row carries the record itself, so the
#: detail pane draws what it already knows how to draw and no field is copied
#: into a third shape that then has to keep up with two.
SearchHit = OptionRecord | SearchablePackage

#: What `kind` answers for an option, and what a row of one is tagged with.
OPTION = "option"

#: What `kind` answers for a package.
PACKAGE = "package"


def kind(hit: SearchHit) -> str:
    """Which index *hit* came from: :data:`OPTION` or :data:`PACKAGE`."""
    return OPTION if isinstance(hit, OptionRecord) else PACKAGE


def name(hit: SearchHit) -> str:
    """What names *hit* in a list.

    An option is its path, and a package is its attribute. `SearchablePackage`
    already calls that `name`, and `OptionRecord` does too, so this reads one
    attribute -- and it stays a function, because a union has no attribute of
    its own that a type checker will accept.
    """
    return hit.name


def make_merged_ranker(
    options: Sequence[OptionRecord] = (),
    packages: Sequence[SearchablePackage] = (),
    *,
    limit: int = RESULTS,
) -> Callable[[str], Sequence[SearchHit]]:
    """Build the ranker that a search over both indexes calls.

    Either sequence may be empty, and an empty one costs nothing: a target
    that holds no options tree still searches packages, and a target that is a
    bare package set still searches packages alone.

    Each source ranks to its own *limit* and the merge takes *limit* of the
    result, so one source cannot crowd the other out of the list entirely
    before the tiers have been compared.
    """
    rank_options = tiered_options(options, limit=limit) if options else None
    rank_packages = tiered_packages(packages, limit=limit) if packages else None

    def rank(query: str) -> Sequence[SearchHit]:
        rows: list[tuple[RankKey, SearchHit]] = []
        if rank_options is not None:
            rows.extend(rank_options(query))
        if rank_packages is not None:
            rows.extend(rank_packages(query))
        rows.sort(key=lambda row: row[0])
        return [hit for _key, hit in rows[:limit]]

    return rank
