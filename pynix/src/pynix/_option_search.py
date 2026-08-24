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
from pynix._ranking import RESULTS, Texts, make_ranker, make_tiered_ranker

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


def rank(records: Sequence[OptionRecord], *, limit: int = RESULTS) -> Callable[[str], Sequence[OptionRecord]]:
    """The function that a search over options alone calls on every keystroke."""
    return make_ranker(records, texts(), limit=limit)


def tiered(
    records: Sequence[OptionRecord],
    *,
    limit: int = RESULTS,
) -> Callable[[str], Sequence[tuple[RankKey, OptionRecord]]]:
    """The same ranking, with the key that a merge sorts on."""
    return make_tiered_ranker(records, texts(), limit=limit)
