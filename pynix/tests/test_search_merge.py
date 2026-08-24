"""Tests for the one order over the option index and the package index.

The merge is a `sorted` call over two lists of `RankKey`, so what these tests
state is that the key really does carry enough to order two sources together,
and that either source may be absent.
"""

from __future__ import annotations

from pynix._options import OptionRecord
from pynix._package_search import SearchablePackage, join
from pynix._packages import PackageRecord
from pynix._search_merge import OPTION, PACKAGE, kind, make_merged_ranker, name


def _option(path: str, description: str = "") -> OptionRecord:
    return OptionRecord(
        name=path,
        type="boolean",
        description=description,
        declarations=[],
        read_only=False,
    )


def _package(attr: str, *, binaries: tuple[str, ...] = (), description: str = "") -> SearchablePackage:
    record = PackageRecord(
        attr=attr,
        pname=attr,
        version="1.0",
        description=description,
        main_program=None,
        broken=False,
        unfree=False,
    )
    return join([record], {attr: list(binaries)})[0]


def test_an_exact_package_beats_a_prefixed_option() -> None:
    """The tier decides, and it decides across the two sources."""
    rank = make_merged_ranker([_option("services.openssh.enable")], [_package("openssh")])
    assert [name(hit) for hit in rank("openssh")] == ["openssh", "services.openssh.enable"]


def test_an_exact_option_component_beats_a_prefixed_package() -> None:
    rank = make_merged_ranker([_option("programs.firefox.enable")], [_package("firefox-esr")])
    assert [name(hit) for hit in rank("firefox")] == ["programs.firefox.enable", "firefox-esr"]


def test_each_row_says_which_index_it_came_from() -> None:
    rank = make_merged_ranker([_option("programs.vscode.enable")], [_package("vscode")])
    assert [kind(hit) for hit in rank("vscode")] == [PACKAGE, OPTION]


def test_a_target_with_no_options_still_searches_packages() -> None:
    """A bare package set is a real thing to point at."""
    rank = make_merged_ranker(packages=[_package("ripgrep", binaries=("rg",))])
    assert [name(hit) for hit in rank("rg")] == ["ripgrep"]


def test_a_target_with_no_packages_still_searches_options() -> None:
    """A module system that hides its package set is a real thing to point at."""
    rank = make_merged_ranker([_option("services.nginx.enable")])
    assert [name(hit) for hit in rank("nginx")] == ["services.nginx.enable"]


def test_a_search_with_neither_index_answers_nothing() -> None:
    assert list(make_merged_ranker()("anything")) == []


def test_a_binary_reaches_a_package_across_the_merge() -> None:
    """The question that `meta.mainProgram` cannot answer, through the merge."""
    rank = make_merged_ranker(
        [_option("services.openssh.enable")],
        [_package("openssh", binaries=("ssh", "ssh-keygen", "sshd"))],
    )
    assert [name(hit) for hit in rank("ssh-keygen")] == ["openssh"]


def test_the_limit_bounds_the_merged_list_and_not_each_source() -> None:
    options = [_option(f"services.thing{index:02d}.enable") for index in range(20)]
    packages = [_package(f"thing{index:02d}") for index in range(20)]
    assert len(make_merged_ranker(options, packages, limit=5)("thing")) == 5


def test_an_empty_query_lists_both_indexes() -> None:
    rank = make_merged_ranker([_option("a.b")], [_package("c")])
    assert sorted(name(hit) for hit in rank("")) == ["a.b", "c"]
