"""Tests for the attribute-path search that the `nix` CLI performs.

Each test names the function of the `nix` source that decides the behaviour,
because the value of this code is that it agrees with that source. A test that
only pinned what this repository does would not notice a disagreement.
"""

from __future__ import annotations

import pytest
from nanopynix_helpers import (
    AttrPathSearch,
    EvaluationTargetError,
    parse_attr_path,
    show_attr_paths,
)

# One command's pair, written out rather than built, so that a change to
# `pynix.target` cannot quietly change what these tests assert.
BASE = AttrPathSearch(
    prefixes=("packages.x86_64-linux.", "legacyPackages.x86_64-linux."),
    defaults=("packages.x86_64-linux.default", "defaultPackage.x86_64-linux"),
)


# --- parse_attr_path: src/libexpr/attr-path.cc ------------------------------


@pytest.mark.parametrize(
    ("attrpath", "parts"),
    [
        ("", ()),
        ("a", ("a",)),
        ("a.b.c", ("a", "b", "c")),
        # A trailing separator adds nothing: Nix appends the last component
        # only when it holds a character.
        ("a.", ("a",)),
        # A separator inside quotation marks is part of the name. This is the
        # only way to reach `legacyPackages."x86_64-linux"`.
        ('a."b.c".d', ("a", "b.c", "d")),
        ('"only"', ("only",)),
        # An empty component in the middle survives the parser, and the
        # selection rejects it later.
        ("a..b", ("a", "", "b")),
        # A leading separator gives an empty first component, for the same
        # reason. The '#.' form never reaches here, because the search strips
        # that character before it parses.
        (".a", ("", "a")),
    ],
)
def test_parse_attr_path(attrpath: str, parts: tuple[str, ...]) -> None:
    assert parse_attr_path(attrpath) == parts


def test_parse_attr_path_rejects_an_unclosed_quote() -> None:
    with pytest.raises(EvaluationTargetError, match="missing closing quote"):
        parse_attr_path('a."b')


# --- show_attr_paths: src/libcmd/installable-flake.cc -----------------------


@pytest.mark.parametrize(
    ("paths", "shown"),
    [
        ((), ""),
        (("a",), "'a'"),
        (("a", "b"), "'a' or 'b'"),
        (("a", "b", "c"), "'a', 'b' or 'c'"),
        (("a", "b", "c", "d"), "'a', 'b', 'c' or 'd'"),
    ],
)
def test_show_attr_paths(paths: tuple[str, ...], shown: str) -> None:
    assert show_attr_paths(paths) == shown


# --- AttrPathSearch.candidates: InstallableFlake::getActualAttrPaths --------


def test_no_fragment_gives_the_defaults() -> None:
    """The constructor keeps `attrPaths` and empties `prefixes` in this case."""
    assert BASE.candidates(None) == (
        "packages.x86_64-linux.default",
        "defaultPackage.x86_64-linux",
    )
    assert BASE.candidates("") == BASE.candidates(None)


def test_a_fragment_is_tried_under_each_prefix_and_then_bare() -> None:
    assert BASE.candidates("hello") == (
        "packages.x86_64-linux.hello",
        "legacyPackages.x86_64-linux.hello",
        "hello",
    )


def test_a_leading_dot_turns_every_prefix_off() -> None:
    """`getActualAttrPaths` erases the character and returns that one path."""
    assert BASE.candidates(".hello") == ("hello",)


def test_a_leading_dot_keeps_the_rest_of_the_path() -> None:
    assert BASE.candidates(".lib.version") == ("lib.version",)


def test_a_search_with_no_prefix_leaves_the_fragment_alone() -> None:
    """`nix fmt` has no prefix, so its fragment names the output exactly."""
    formatter = AttrPathSearch(prefixes=(), defaults=("formatter.x86_64-linux",))

    assert formatter.candidates("something") == ("something",)
    assert formatter.candidates(None) == ("formatter.x86_64-linux",)


def test_an_empty_search_selects_nothing() -> None:
    """A command that `nix` has not got passes no lists, and reads no default."""
    assert AttrPathSearch().candidates(None) == ()
    assert AttrPathSearch().candidates("hello") == ("hello",)
