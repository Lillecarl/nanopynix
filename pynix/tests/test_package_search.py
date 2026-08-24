"""Tests for the join of the two package sources, and for the ranking over it.

These are pure functions over records, so the tests build the records directly
and need no evaluator. The values are copied from the real indexes: `openssh`
really installs `ssh-keygen` without naming it as its `mainProgram`, and `erg`
and `rgl` really are the packages that crowded out `ripgrep` for the query
`rg` before the ranking gained its tiers.
"""

from __future__ import annotations

from pynix._package_search import SearchablePackage, join, rank
from pynix._packages import PackageRecord


def _record(
    attr: str, *, pname: str | None = None, main: str | None = None, description: str | None = None
) -> PackageRecord:
    return PackageRecord(
        attr=attr,
        pname=pname if pname is not None else attr,
        version="1.0",
        description=description,
        main_program=main,
        broken=False,
        unfree=False,
    )


def test_the_join_attaches_the_binaries_of_a_package() -> None:
    packages = join([_record("openssh", main="ssh")], ({"openssh": ["ssh", "ssh-keygen", "sshd"]}))
    assert packages[0].binaries == ("ssh", "ssh-keygen", "sshd")


def test_a_package_the_channel_does_not_know_still_joins() -> None:
    """The two sources describe different things, so a miss is not an error.

    The walk reads the nixpkgs the caller pinned, and the index reads one
    release. A package the release does not carry keeps its metadata.
    """
    packages = join([_record("my-local-thing")], ({}))
    assert packages[0].binaries == ()
    assert packages[0].name == "my-local-thing"


def test_the_join_works_with_no_index_at_all() -> None:
    packages = join([_record("ripgrep", main="rg")])
    assert packages[0].binaries == ()
    assert packages[0].command == "rg"


def test_the_haystack_holds_every_text_a_query_can_match() -> None:
    package = join(
        [_record("openssh", pname="openssh", description="An SSH implementation")],
        ({"openssh": ["ssh", "ssh-keygen"]}),
    )[0]
    haystack = package.haystack
    for text in ("openssh", "ssh-keygen", "An SSH implementation"):
        assert text in haystack


def test_the_command_prefers_what_the_package_says_itself() -> None:
    package = join([_record("ripgrep", main="rg")], ({"ripgrep": ["rg", "rg-helper"]}))[0]
    assert package.command == "rg"


def test_one_binary_and_no_main_program_is_the_command() -> None:
    package = join([_record("hello")], ({"hello": ["hello"]}))[0]
    assert package.command == "hello"


def test_several_binaries_and_no_main_program_names_none() -> None:
    """The package has not said which one to run, so this does not guess."""
    package = join([_record("coreutils")], ({"coreutils": ["ls", "cp", "mv"]}))[0]
    assert package.command is None


def _ranked(packages: list[SearchablePackage], query: str) -> list[str]:
    return [package.name for package in rank(packages)(query)]


def test_an_exact_binary_beats_a_fuzzy_name() -> None:
    """Regression test, and the headline of issue #85.

    Before the tiers, `rg` over the real 24 571 packages gave 500 results led
    by `erg` and `rgl`: the haystack held the binary, so "contains rg" matched
    everywhere, and the order came from the attribute alone. A binary either
    *is* `rg` or it is not.
    """
    packages = join(
        [_record("ripgrep", main="rg"), _record("erg"), _record("rgl")],
        ({"ripgrep": ["rg"], "erg": ["erg"], "rgl": ["rgl"]}),
    )
    assert _ranked(packages, "rg")[0] == "ripgrep"


def test_a_binary_that_is_not_a_main_program_still_wins() -> None:
    """`openssh` names `ssh`, and the caller asked for `ssh-keygen`."""
    packages = join(
        [_record("openssh", main="ssh"), _record("ssh-keygen-helper")],
        ({"openssh": ["ssh", "ssh-keygen"]}),
    )
    assert _ranked(packages, "ssh-keygen")[0] == "openssh"


def test_the_shorter_attribute_wins_inside_a_tier() -> None:
    """Three real packages install `ssh-keygen`, and one is the one meant."""
    packages = join(
        [_record("opensshWithKerberos"), _record("openssh"), _record("opensshTest")],
        {name: ["ssh-keygen"] for name in ("openssh", "opensshTest", "opensshWithKerberos")},
    )
    assert _ranked(packages, "ssh-keygen")[:2] == ["openssh", "opensshTest"]


def test_an_exact_name_beats_an_exact_binary() -> None:
    """A caller who types a package name means that package."""
    packages = join(
        [_record("vim"), _record("xxd")],
        ({"vim": ["vim", "xxd"], "xxd": ["xxd"]}),
    )
    assert _ranked(packages, "xxd")[0] == "xxd"


def test_a_package_appears_once_however_many_tiers_hold_it() -> None:
    packages = join([_record("hello")], ({"hello": ["hello"]}))
    assert _ranked(packages, "hello") == ["hello"]


def test_a_description_still_finds_a_package() -> None:
    packages = join([_record("ripgrep", main="rg", description="Recursively search directories")])
    assert _ranked(packages, "recursively") == ["ripgrep"]


def test_an_empty_query_lists_the_packages() -> None:
    packages = join([_record("b"), _record("a")])
    assert _ranked(packages, "") == ["a", "b"]
