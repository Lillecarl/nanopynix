"""Tests for the two completion rules that the `nix` CLI applies.

Each test names the function of the `nix` source that decides the behaviour,
for the reason `test_attr_path_search.py` gives: the value of this code is that
it agrees with that source.

**These drive a double, and the equivalence against `nix` itself is measured
elsewhere.** `pynix/completions/tests/test_nix_equivalence.py` asks a real
`nix` through `NIX_GET_COMPLETIONS` and asks a real `pynix` through
argcomplete, on a pty. That suite is the proof; this one states each rule on
its own, so a failure names the rule rather than the whole answer.
"""

# ruff: noqa: ASYNC109
# The double below subclasses the real ValueProxy, whose async methods take a
# `timeout` keyword; an override has to keep it. Same exemption, same reason
# as test_fod.py.

from __future__ import annotations

import pytest
from nanopynix_helpers import (
    AttrPathSearch,
    complete_file_attr_path,
    complete_flake_fragment,
)

from nanopynix.exceptions import NixTypeError
from nanopynix.rpc import ValueProxy

#: One command's pair, written out rather than built, so that a change to
#: `pynix.target` cannot quietly change what these tests assert. This is the
#: pair of `SourceExprCommand`, which `nix build` and `nix eval` use.
BASE = AttrPathSearch(
    prefixes=("packages.x86_64-linux.", "legacyPackages.x86_64-linux."),
    defaults=("packages.x86_64-linux.default", "defaultPackage.x86_64-linux"),
)

#: The pair of `nix develop`, from `src/nix/develop.cc`.
DEV_SHELL = AttrPathSearch(
    prefixes=("devShells.x86_64-linux.", *BASE.prefixes),
    defaults=("devShells.x86_64-linux.default", "devShell.x86_64-linux", *BASE.defaults),
)


#: A nested attribute set of strings, which is every shape these tests need.
type _Contents = dict[str, "_Contents"] | str


class _Tree(ValueProxy):
    """A nested attribute set, and nothing else a value can be.

    A subclass of `ValueProxy` and not a duck type, because beartype checks
    each annotated parameter with `isinstance`. `test_fod.py` says the same.

    A leaf is anything that is not a `dict`, and asking a leaf for its names
    raises :class:`~nanopynix.exceptions.NixTypeError`, which is what the real
    value does. That is the case Nix guards with `if (v2.type() == nAttrs)`.
    """

    def __init__(self, contents: _Contents) -> None:
        self._contents = contents

    async def attr_names(self, *, timeout: float | None = None) -> list[str]:
        if not isinstance(self._contents, dict):
            raise NixTypeError("TypeError", "value is not an attribute set")
        return list(self._contents)

    async def has_attr(self, name: str, *, timeout: float | None = None) -> bool:
        if not isinstance(self._contents, dict):
            raise NixTypeError("TypeError", "value is not an attribute set")
        return name in self._contents

    def attr(self, name: str, *, timeout: float | None = None) -> _Tree:
        contents = self._contents
        if not isinstance(contents, dict):
            raise NixTypeError("TypeError", "value is not an attribute set")
        return _Tree(contents[name])


#: The outputs of a flake that holds a name under each root of the search.
#:
#: `pkgone` is under two roots on purpose: Nix collects its candidates in a
#: `std::set`, so a name that two roots hold appears once.
FLAKE = _Tree(
    {
        "packages": {"x86_64-linux": {"pkgone": "one", "pkgtwo": "two", "default": "def"}},
        "legacyPackages": {"x86_64-linux": {"legone": "leg", "pkgone": "shadowed"}},
        "devShells": {"x86_64-linux": {"shellone": "sh"}},
        "topone": "top",
        "toptwo": {"deep": "d"},
    }
)

#: A file that holds a name with a dot in it, which only quotation marks reach.
FILE = _Tree({"nixos": {"config": {"system": "s"}}, "nixosLater": {"marker": "m"}, "a.b": {"inner": "i"}})


# --- the --file branch: SourceExprCommand::completeInstallable --------------


@pytest.mark.parametrize(
    ("prefix", "candidates"),
    [
        # Nothing typed lists the top of the file.
        ("", ["a.b", "nixos", "nixosLater"]),
        # A stem matches by prefix, and the candidate is the whole path.
        ("nixo", ["nixos", "nixosLater"]),
        ("nixosL", ["nixosLater"]),
        # A trailing dot means the component before it is finished.
        ("nixos.", ["nixos.config"]),
        ("nixos.config.", ["nixos.config.system"]),
        # A path that leads nowhere offers nothing rather than failing.
        ("nixos.absent.", []),
        # A leaf is not an attribute set, and Nix offers nothing for one.
        ("nixos.config.system.", []),
    ],
)
async def test_the_file_branch_offers_the_whole_path(prefix: str, candidates: list[str]) -> None:
    assert await complete_file_attr_path(FILE, prefix) == candidates


async def test_the_file_branch_keeps_the_quotation_marks_the_caller_typed() -> None:
    """Nix splits on the last literal dot and concatenates that text again.

    So the left half of the candidate is the caller's own spelling, and a
    component that holds a dot survives. The flake branch does not do this,
    and `test_the_flake_branch_rebuilds_the_path_from_the_names` is the
    counterpart that states the difference.
    """
    assert await complete_file_attr_path(FILE, '"a.b".') == ['"a.b".inner']


# --- the flake branch: completeFlakeRefWithFragment -------------------------


async def test_an_empty_fragment_offers_the_union_of_every_root() -> None:
    """Nix pushes the empty prefix onto the list, so the top of the flake is one root."""
    assert await complete_flake_fragment(FLAKE, BASE, "") == [
        "",
        "default",
        "devShells",
        "legacyPackages",
        "legone",
        "packages",
        "pkgone",
        "pkgtwo",
        "topone",
        "toptwo",
    ]


async def test_a_name_that_two_roots_hold_appears_once() -> None:
    """`pkgone` is under `packages` and under `legacyPackages`.

    Nix collects into a `std::set`, so the caller sees one candidate.
    """
    assert await complete_flake_fragment(FLAKE, BASE, "pkgone") == ["pkgone"]


async def test_the_search_of_the_command_decides_what_is_offered() -> None:
    """`nix develop` prefixes `devShells.<system>` and `nix build` does not."""
    assert await complete_flake_fragment(FLAKE, BASE, "shell") == []
    assert await complete_flake_fragment(FLAKE, DEV_SHELL, "shell") == ["shellone"]


async def test_a_leading_dot_clears_the_prefixes_and_stays_in_each_candidate() -> None:
    """`#.hello` reaches a top-level output that a prefix would otherwise hide."""
    assert await complete_flake_fragment(FLAKE, BASE, ".top") == [".topone", ".toptwo"]


async def test_an_empty_fragment_offers_itself_when_a_default_resolves() -> None:
    """`nix build F#` offers `F#`, because `packages.<system>.default` is there.

    The empty string is that candidate: the caller's word, unchanged. A flake
    with no default offers no such candidate, which is the second assertion.
    """
    assert "" in await complete_flake_fragment(FLAKE, BASE, "")

    without_default = _Tree({"packages": {"x86_64-linux": {"pkgone": "one"}}})
    assert "" not in await complete_flake_fragment(without_default, BASE, "")


async def test_the_flake_branch_rebuilds_the_path_from_the_names() -> None:
    """A nested fragment comes back with its prefix removed and nothing else.

    `packages.x86_64-linux.toptwo` does not exist, so only the top-level root
    answers, and the candidate is `toptwo.deep` rather than the path it was
    found under.
    """
    assert await complete_flake_fragment(FLAKE, BASE, "toptwo.") == ["toptwo.deep"]


async def test_the_empty_search_reads_the_fragment_as_one_path() -> None:
    """`pynix search` passes no search, and no search applies no prefix.

    It also offers no empty candidate, because it has no default to resolve.
    """
    assert await complete_flake_fragment(FLAKE, AttrPathSearch(), "") == [
        "devShells",
        "legacyPackages",
        "packages",
        "topone",
        "toptwo",
    ]
