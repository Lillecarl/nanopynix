"""Join the two package sources, and rank the result.

`pynix/_packages.py` walks the nixpkgs the caller pinned and gives the
metadata. `pynix/_programs.py` reads the channel index and gives the binaries.
This module joins them and says what a query searches.

**A package matches on more than its name, and that is the point.** Issue #85
calls "which package gives me `rg`" the single largest improvement a package
search can make. So the haystack holds the attribute, the pname, the
description and every binary the package installs, while the name that decides
the order stays the attribute alone. `pynix/_ranking.py` takes the two apart
for this reason: ordering by a text that holds a description would sink every
package that has one.

**The join is on the attribute, and a miss is not an error.** The two sources
describe different things: the walk describes the caller's own pin, and the
channel index describes one release of nixpkgs. A package that the release does
not carry simply has no binaries recorded, and the search still answers for it
from its metadata.

This module imports no `prompt_toolkit`, so a caller that prints a list rather
than drawing a screen pays for none of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING
from pynix._ranking import Texts, make_ranker

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Mapping, Sequence

    from pynix._packages import PackageRecord


@dataclass(frozen=True)
class SearchablePackage:
    """One package, with everything a query can match against it."""

    record: PackageRecord

    #: The binaries the package installs, in order. Empty when the channel
    #: index knows nothing about it, which is not the same as "installs none".
    binaries: tuple[str, ...]

    @property
    def name(self) -> str:
        """The text that names the package, and that decides the order."""
        return self.record.attr

    @property
    def haystack(self) -> str:
        """Every text a query may be found in.

        The order matters to nothing: the ranking asks only whether each word
        of the query is in here.
        """
        parts = [self.record.attr, self.record.pname, *self.binaries]
        if self.record.description:
            parts.append(self.record.description)
        return " ".join(parts)

    @property
    def command(self) -> str | None:
        """What the caller types to run the program, when that is knowable.

        `meta.mainProgram` is the package saying so itself, and it wins. A
        package that names none but installs exactly one binary has said it
        another way. One that installs several has not said it at all, and
        this answers `None` rather than guessing.
        """
        if self.record.main_program:
            return self.record.main_program
        if len(self.binaries) == 1:
            return self.binaries[0]
        return None


def join(
    records: Sequence[PackageRecord],
    binaries: Mapping[str, Sequence[str]] | None = None,
) -> list[SearchablePackage]:
    """Attach *binaries* to *records*, by attribute.

    *binaries* is what `ProgramIndex.binaries_by_package` returns, and this
    takes the mapping rather than the index: the join is the subject here, and
    where the rows came from is not. A test then needs no database.

    It is optional as well. Without it every package still searches on its
    attribute, its pname and its description, and `rg` still finds `ripgrep`
    through `meta.mainProgram`. What is lost is every binary that is not the
    main one, which is `ssh-keygen`, `convert` and `awk`.
    """
    known: Mapping[str, Sequence[str]] = binaries if binaries is not None else {}
    return [SearchablePackage(record=record, binaries=tuple(known.get(record.attr, ()))) for record in records]


def package_keys(package: SearchablePackage) -> Sequence[str]:
    """The texts that name one package: its attribute and its pname."""
    return (package.record.attr, package.record.pname)


def package_aliases(package: SearchablePackage) -> Sequence[str]:
    """Every binary the package installs.

    These are the whole reason `pynix/_programs.py` exists: `ssh-keygen` is
    not the `mainProgram` of `openssh`, so nothing else reaches it.

    **An alias, and not a key.** `vim` installs `xxd`, and so does `xxd`. A
    person who types `xxd` means the package of that name, and the two names
    are the same length, so only a lower tier can put the alias second.
    """
    return package.binaries


def rank(packages: Sequence[SearchablePackage]) -> Callable[[str], Sequence[SearchablePackage]]:
    """Return the function that a search calls on every keystroke.

    **An exact match on a name or on a binary comes before any fuzzy one.**
    Measured before the tiers, over the real 24 571 packages: `rg` gave 500
    results led by `erg` and `rgl`, `convert` gave 500 led by `convertx`, and
    `ssh-keygen` put `opensshWithKerberos` above `openssh`. Issue #85 asks
    instead that `pynix search rg` put `ripgrep` in the first three.

    `pynix._ranking` holds the tiers now, and this function names the keys and
    the haystack that they read. It held its own three tiers until issue #257,
    which needed the same order over options as well, so that one list can
    hold both.
    """
    return make_ranker(
        packages,
        Texts(
            name=lambda package: package.name,
            keys=package_keys,
            aliases=package_aliases,
            haystack=lambda package: package.haystack,
        ),
    )
