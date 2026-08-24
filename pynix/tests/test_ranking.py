"""Tests for the tiers that every `pynix search` shares.

Issue #257 needed one order across two sources, and measured that a
`rapidfuzz` score cannot give one: `WRatio` saturates at 90 for almost every
substring hit, so nearly every match on both sides carries the same number.
The tier is what orders a match, and the score only breaks a tie inside one.
These tests state each tier and the boundary between two of them.
"""

from __future__ import annotations

from dataclasses import dataclass

from pynix._ranking import ALIAS, EXACT, FUZZY, PREFIX, WORDS, Texts, make_ranker, make_tiered_ranker


@dataclass(frozen=True)
class _Thing:
    name: str
    also: tuple[str, ...] = ()
    text: str = ""

    @property
    def haystack(self) -> str:
        return f"{self.name} {' '.join(self.also)} {self.text}"


def _texts() -> Texts[_Thing]:
    return Texts(
        name=lambda thing: thing.name,
        keys=lambda thing: (thing.name, *thing.name.split(".")),
        aliases=lambda thing: thing.also,
        haystack=lambda thing: thing.haystack,
    )


def _tiers(things: list[_Thing], query: str) -> list[tuple[int, str]]:
    ranked = make_tiered_ranker(things, _texts())(query)
    return [(key[0], item.name) for key, item in ranked]


def _names(things: list[_Thing], query: str) -> list[str]:
    return [thing.name for thing in make_ranker(things, _texts())(query)]


def test_an_exact_name_is_the_first_tier() -> None:
    things = [_Thing("vim"), _Thing("vim-full")]
    assert _tiers(things, "vim")[0] == (EXACT, "vim")


def test_an_alias_ranks_below_a_name_of_the_same_length() -> None:
    """`vim` installs `xxd`, and so does `xxd`. A person means the package.

    Both names are three characters, so no tie-break inside one tier can
    decide this. Only a lower tier for the alias puts `xxd` first.
    """
    things = [_Thing("vim", also=("vim", "xxd")), _Thing("xxd", also=("xxd",))]
    assert _tiers(things, "xxd") == [(EXACT, "xxd"), (ALIAS, "vim")]


def test_a_prefix_ranks_below_every_exact_match() -> None:
    things = [_Thing("openssh"), _Thing("openssh-with-kerberos"), _Thing("other", also=("openssh",))]
    assert _tiers(things, "openssh") == [
        (EXACT, "openssh"),
        (ALIAS, "other"),
        (PREFIX, "openssh-with-kerberos"),
    ]


def test_a_prefix_reads_an_alias_as_well() -> None:
    """`ssh-key` must still reach the package that installs `ssh-keygen`."""
    things = [_Thing("openssh", also=("ssh-keygen",))]
    assert _tiers(things, "ssh-key") == [(PREFIX, "openssh")]


def test_a_word_of_the_query_reaches_the_haystack_alone() -> None:
    things = [_Thing("ripgrep", text="recursively search directories")]
    assert _tiers(things, "recursively") == [(WORDS, "ripgrep")]


def test_a_component_of_a_dotted_name_is_an_exact_match() -> None:
    """A person types `openssh`, and the option is `services.openssh.enable`.

    Without the components that query reaches the option through the word
    filter alone, which orders by the alphabet. Measured over 14 752 real
    options, that put `services.openssh.allowSFTP` first.
    """
    things = [_Thing("services.openssh.allowSFTP"), _Thing("services.openssh.enable")]
    assert [name for _tier, name in _tiers(things, "openssh")] == [
        "services.openssh.enable",
        "services.openssh.allowSFTP",
    ]


def test_a_typo_falls_through_to_the_fuzzy_tier() -> None:
    things = [_Thing("vscode")]
    assert _tiers(things, "vscodee") == [(FUZZY, "vscode")]


def test_a_query_that_matches_nothing_answers_nothing() -> None:
    assert _names([_Thing("hello"), _Thing("world")], "zzzzzzzz") == []


def test_a_record_appears_once_however_many_tiers_hold_it() -> None:
    """A name, an alias and the haystack can all hold the query at once."""
    things = [_Thing("hello", also=("hello",), text="hello hello")]
    assert _names(things, "hello") == ["hello"]


def test_the_shorter_name_wins_inside_a_tier() -> None:
    things = [_Thing("aaa-long-name"), _Thing("aaa-x")]
    assert _names(things, "aaa") == ["aaa-x", "aaa-long-name"]


def test_one_more_letter_never_gives_more_results() -> None:
    """The rule that the two-stage filter exists for, over the tiers now."""
    things = [_Thing(name) for name in ("vsce", "vscode", "vscodium", "code", "unrelated")]
    counts = [len(_names(things, query)) for query in ("vsc", "vsco", "vscod", "vscode")]
    assert counts == sorted(counts, reverse=True)


def test_an_empty_query_lists_the_records_by_name() -> None:
    assert _names([_Thing("b"), _Thing("a")], "") == ["a", "b"]


def test_the_limit_bounds_every_tier_together() -> None:
    things = [_Thing(f"thing-{index:03d}") for index in range(50)]
    assert len(make_ranker(things, _texts(), limit=7)("thing")) == 7


def test_a_key_defaults_to_the_name_and_an_alias_to_nothing() -> None:
    """A caller that gives only a name still gets the tiers."""
    things = [_Thing("alpha"), _Thing("alphabet")]
    ranked = make_tiered_ranker(things, Texts(name=lambda thing: thing.name))("alpha")
    assert [(key[0], item.name) for key, item in ranked] == [(EXACT, "alpha"), (PREFIX, "alphabet")]


def test_the_key_of_a_match_orders_two_sources_together() -> None:
    """The reason a key comes back at all: one list holds both searches."""
    left = [_Thing("openssh")]
    right = [_Thing("services.openssh.enable"), _Thing("programs.ssh.extraConfig")]
    merged = [
        *make_tiered_ranker(left, _texts())("openssh"),
        *make_tiered_ranker(right, _texts())("openssh"),
    ]
    merged.sort(key=lambda row: row[0])
    assert [item.name for _key, item in merged] == ["openssh", "services.openssh.enable"]
