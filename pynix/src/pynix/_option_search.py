"""What a query matches in one NixOS option, and in what order.

`pynix/_package_search.py` answers the same question for a package, and this
module is its twin. The pair exists so that `pynix search` can read both
indexes and put the answers in one list.

**This module imports no `prompt_toolkit`.** The ranking belonged to
`pynix._impl.options_tui` until issue #257, which is the module that draws the
screen and therefore carries 115 `prompt_toolkit` modules and 69 Markdown
ones. A caller who prints a list pays for none of them, and a caller who
merges two sources cannot import the screen at all.

**Every component of the path is a key.** An option is named
`services.openssh.enable`, and a person types `openssh`. Without the
components that query reaches the option through the word filter alone, which
orders by the alphabet: measured over 14 752 real options, `openssh` put
`services.openssh.allowSFTP` above `services.openssh.enable`.

An option has no alias. A package answers to every binary it installs, and an
option answers to its own name and nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING
from pynix._option_paths import SEPARATOR, Instance, bind, split_path
from pynix._ranking import ALIAS, PREFIX, RESULTS, Texts, make_tiered_ranker

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Sequence

    from pynix._options import OptionRecord
    from pynix._ranking import RankKey


def option_keys(record: OptionRecord) -> Sequence[str]:
    """The whole path of the option, and each component of it."""
    return (record.name, *record.name.split("."))


def texts() -> Texts[OptionRecord]:
    """How a ranker reads one option.

    The haystack is the name, so an option matches on its name alone. A
    description would widen it, and a description is Markdown prose: measured
    over the real index, every option that carries one would match almost any
    English word.
    """
    return Texts(name=lambda record: record.name, keys=option_keys)


#: The score that a placeholder match carries. It is the fixed one that every
#: tier above `WORDS` uses, because the tier has already ordered the match.
_CERTAIN = 100.0


def _placeholder_index(records: Sequence[OptionRecord]) -> list[tuple[list[str], OptionRecord]]:
    """Each option that stands for many, with its path split once.

    Splitting on every keystroke would cost the whole index. Only an option
    that carries a placeholder can match this way, and that is 3 774 of the
    24 941 in one real configuration, so the pre-filter is most of the work.
    """
    return [(split_path(record.name), record) for record in records if "<" in record.name]


def _instances(
    index: Sequence[tuple[list[str], OptionRecord]],
    query: str,
) -> list[tuple[RankKey, OptionRecord]]:
    """Every option that *query* names an instance of, with its rank key.

    **A whole match is an alias and not an exact match.** The reader typed a
    path that no record carries literally, and the record stands in for it,
    which is what the alias tier means. An option whose own name equals the
    query still ranks above it.
    """
    typed = split_path(query)
    found: list[tuple[RankKey, OptionRecord]] = []
    for segments, record in index:
        match = bind(segments, typed)
        if match is None:
            continue
        tier = ALIAS if match.whole else PREFIX
        found.append(((tier, -_CERTAIN, len(record.name), record.name), record))
    return found


def instance_of(record: OptionRecord, query: str) -> Instance | None:
    """What *query* binds the placeholders of *record* to, or `None`.

    The interface calls this for the one option a reader selected, so that it
    can name the concrete path. Issue #266 reads the value at that path.
    """
    return bind(split_path(record.name), split_path(query))


def tiered(
    records: Sequence[OptionRecord],
    *,
    limit: int = RESULTS,
) -> Callable[[str], Sequence[tuple[RankKey, OptionRecord]]]:
    """The ranking, with the key that a merge sorts on."""
    plain = make_tiered_ranker(records, texts(), limit=limit)
    index = _placeholder_index(records)

    def rank_all(query: str) -> Sequence[tuple[RankKey, OptionRecord]]:
        base = plain(query)
        # A query with no separator names no path, so no placeholder can
        # stand in for part of it.
        if SEPARATOR not in query:
            return base
        instances = _instances(index, query)
        if not instances:
            return base
        promoted = {record.name for _key, record in instances}
        merged = [*instances, *((key, record) for key, record in base if record.name not in promoted)]
        merged.sort(key=lambda pair: pair[0])
        return merged[:limit]

    return rank_all


def rank(records: Sequence[OptionRecord], *, limit: int = RESULTS) -> Callable[[str], Sequence[OptionRecord]]:
    """The function that a search over options alone calls on every keystroke."""
    ordered = tiered(records, limit=limit)

    def rank_names(query: str) -> Sequence[OptionRecord]:
        return [record for _key, record in ordered(query)]

    return rank_names
