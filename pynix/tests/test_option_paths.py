"""What a concrete attribute path matches, and what it binds."""

from __future__ import annotations

import pytest

from pynix._option_paths import bind, is_placeholder, split_path
from pynix._option_search import instance_of, tiered
from pynix._options import OptionRecord
from pynix._ranking import ALIAS, EXACT, PREFIX


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("systemd.services.asdf.requires", ["systemd", "services", "asdf", "requires"]),
        ("single", ["single"]),
        # **A key that holds a dot is quoted, and stays one segment.** This is
        # the case that a plain `text.split(".")` gets wrong, and getting it
        # wrong puts every segment after it out of step.
        (
            'services.nginx.virtualHosts."example.com".root',
            ["services", "nginx", "virtualHosts", "example.com", "root"],
        ),
        ('"a.b"', ["a.b"]),
        ('one."two.three".four', ["one", "two.three", "four"]),
    ],
)
def test_a_path_splits_on_a_dot_outside_quotes(text: str, expected: list[str]) -> None:
    assert split_path(text) == expected


@pytest.mark.parametrize(
    ("segment", "expected"),
    [("<name>", True), ("<anything>", True), ("name", False), ("<>", False), ("<a", False), ("a>", False)],
)
def test_a_placeholder_is_the_brackets_and_not_the_word(segment: str, expected: bool) -> None:
    """A submodule names its key what it likes, so the brackets are the test."""
    assert is_placeholder(segment) is expected


def test_a_whole_path_binds_every_placeholder() -> None:
    match = bind(split_path("systemd.services.<name>.requires"), split_path("systemd.services.asdf.requires"))
    assert match is not None
    assert match.whole
    assert match.bound == (("<name>", "asdf"),)
    assert match.path == "systemd.services.asdf.requires"


def test_a_quoted_key_binds_and_comes_back_quoted() -> None:
    """The round trip matters: #266 reads the value at the path this gives."""
    typed = 'services.nginx.virtualHosts."example.com".root'
    match = bind(split_path("services.nginx.virtualHosts.<name>.root"), split_path(typed))
    assert match is not None
    assert match.bound == (("<name>", "example.com"),)
    assert match.path == typed


def test_a_shorter_query_matches_the_front_and_says_so() -> None:
    """A reader part-way through typing still reaches the option."""
    match = bind(split_path("systemd.services.<name>.requires"), split_path("systemd.services.asdf"))
    assert match is not None
    assert not match.whole
    assert match.bound == (("<name>", "asdf"),)


@pytest.mark.parametrize(
    ("option", "query"),
    [
        ("systemd.services.<name>.requires", "systemd.timers.asdf.requires"),
        ("systemd.services.<name>.requires", "systemd.services.asdf.requires.extra"),
        ("systemd.services.<name>.requires", ""),
        ("systemd.services.name.requires", "systemd.services.asdf.requires"),
    ],
)
def test_a_path_that_does_not_line_up_binds_nothing(option: str, query: str) -> None:
    assert bind(split_path(option), split_path(query)) is None


def _option(name: str) -> OptionRecord:
    return OptionRecord(name=name, type="boolean", description=None, declarations=[], read_only=False)


_INDEX = [
    _option("systemd.services.<name>.requires"),
    _option("systemd.services.<name>.requiredBy"),
    _option("systemd.services"),
    _option("systemd.user.services.<name>.requires"),
    _option("services.nginx.virtualHosts.<name>.root"),
    _option("services.nginx.virtualHosts.<name>.acmeRoot"),
    _option("services.nginx.virtualHosts"),
    _option("services.openssh.enable"),
]


def test_an_instance_path_puts_the_option_it_names_first() -> None:
    """Regression test for the whole point of this module.

    Measured over one real configuration before it existed:
    `systemd.services.asdf.requires` put `systemd.services.<name>.requires`
    second, in the fuzzy tier, under a bare `systemd.services`.
    """
    hits = tiered(_INDEX)("systemd.services.asdf.requires")
    (key, record) = hits[0]
    assert record.name == "systemd.services.<name>.requires"
    assert key[0] == ALIAS


def test_a_quoted_key_that_holds_a_dot_reaches_its_option() -> None:
    """`example.com` is one key and two dots, so the split has to be quote-aware."""
    hits = tiered(_INDEX)('services.nginx.virtualHosts."example.com".root')
    assert hits[0][1].name == "services.nginx.virtualHosts.<name>.root"
    assert hits[0][0][0] == ALIAS


def test_a_part_typed_instance_path_ranks_as_a_prefix() -> None:
    """A reader mid-word reaches the sub-options, below a whole match."""
    hits = tiered(_INDEX)("systemd.services.asdf")
    names = [record.name for _key, record in hits]
    assert "systemd.services.<name>.requires" in names
    assert all(key[0] >= PREFIX for key, record in hits if "<name>" in record.name)


def test_an_option_named_exactly_still_beats_a_stand_in() -> None:
    """A record whose own name is the query is not a stand-in for anything."""
    hits = tiered(_INDEX)("services.openssh.enable")
    assert hits[0][1].name == "services.openssh.enable"
    assert hits[0][0][0] == EXACT


def test_a_query_with_no_dot_ranks_as_it_did() -> None:
    """No separator names no path, so no placeholder can stand in for one."""
    hits = tiered(_INDEX)("requires")
    assert hits
    assert all(key[0] != ALIAS for key, _record in hits)


def test_the_ranking_names_each_option_once() -> None:
    """A promoted record must not also appear from the ordinary ranking."""
    hits = tiered(_INDEX)("systemd.services.asdf.requires")
    names = [record.name for _key, record in hits]
    assert len(names) == len(set(names))


def test_the_interface_can_ask_what_the_selection_bound() -> None:
    """#266 reads a value at the concrete path, so it needs the binding."""
    match = instance_of(_option("systemd.services.<name>.requires"), "systemd.services.asdf.requires")
    assert match is not None
    assert match.path == "systemd.services.asdf.requires"
    assert instance_of(_option("services.openssh.enable"), "systemd.services.asdf") is None
