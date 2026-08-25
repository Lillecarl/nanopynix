"""Tests for the bulk package walk that `pynix search` reads.

The fixture is a real attribute set: `lib` and `pkgs` come from this
repository, and each package in it is a real derivation. So the walk under test
runs against the thing it will meet, and not against a double.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pynix._packages import (
    PackageRecord,
    cache_path,
    fetch_package_list,
    indexed_packages,
    load_cache,
    package_identity,
    save_cache,
)
from pynix._util import eval_session
from pynix.target import EvaluationTarget, evaluate_target, select_attr

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import NixTestEnvironment

_PKGSET = Path(__file__).parent / "test_packages" / "pkgset.nix"


@pytest.fixture
async def packages(shared_nix_environment: NixTestEnvironment) -> list[PackageRecord]:
    """Every package of the fixture set, through the real walk."""
    target = EvaluationTarget(file=str(_PKGSET), attr=None, flake=None)
    async with eval_session(shared_nix_environment.store_uri) as (_nix, _store, session):
        root = await evaluate_target(target, session, auto_call_file=True)
        lib_value = await select_attr(root, "lib")
        return await fetch_package_list(session, root, lib_value)


def _by_attr(records: list[PackageRecord], attr: str) -> PackageRecord:
    return next(record for record in records if record.attr == attr)


def test_the_walk_reads_a_package_and_its_main_program(packages: list[PackageRecord]) -> None:
    record = _by_attr(packages, "ripgrep")
    assert record.pname == "ripgrep"
    assert record.version == "14.1.1"
    assert record.main_program == "rg"
    assert record.description is not None
    assert "regex pattern" in record.description


def test_a_package_that_names_no_main_program_still_records(packages: list[PackageRecord]) -> None:
    """8 502 of 24 571 real packages name none, so this is the common case."""
    record = _by_attr(packages, "hello-no-main")
    assert record.pname == "hello"
    assert record.main_program is None


def test_a_package_that_throws_does_not_end_the_walk(packages: list[PackageRecord]) -> None:
    """`builtins.tryEval` guards each attribute, and this is what it buys.

    The walk returns one Nix list, forced in one pass, so an attribute that
    raises would otherwise take every other package with it.
    """
    attrs = {record.attr for record in packages}
    assert "throwing" not in attrs
    assert {"ripgrep", "hello-no-main", "broken", "unfree"} <= attrs


def test_an_attribute_that_is_not_a_derivation_is_skipped(packages: list[PackageRecord]) -> None:
    attrs = {record.attr for record in packages}
    assert "notADerivation" not in attrs
    # `lib` and `pkgs` are in the set too, and neither is a package.
    assert "lib" not in attrs
    assert "pkgs" not in attrs


def test_the_walk_records_broken_and_unfree(packages: list[PackageRecord]) -> None:
    """A search filters on both, so the walk has to carry both."""
    assert _by_attr(packages, "broken").broken is True
    assert _by_attr(packages, "ripgrep").broken is False

    assert _by_attr(packages, "unfree").unfree is True
    assert _by_attr(packages, "ripgrep").unfree is False


def test_the_walk_forces_no_store_path(packages: list[PackageRecord]) -> None:
    """A record carries no output path, on purpose.

    Resolving a path means instantiating the derivation, and a search that
    answers in milliseconds cannot do that for 24 571 packages.
    """
    record = _by_attr(packages, "ripgrep")
    assert not hasattr(record, "out_path")
    assert not hasattr(record, "store_path")


# -- the cache -----------------------------------------------------------------
#
# The key is a store path, so the cache cannot go stale: evaluation is pure, so
# the same nixpkgs gives the same walk for ever. A miss is never an error, and
# every one of these cases makes the caller walk again.


def _some_records() -> list[PackageRecord]:
    return [
        PackageRecord(
            attr="ripgrep",
            pname="ripgrep",
            version="14.1.1",
            description="Recursively search directories",
            main_program="rg",
            broken=False,
            unfree=False,
        ),
        PackageRecord(
            attr="unfree-thing",
            pname="unfree-thing",
            version="1.0",
            description=None,
            main_program=None,
            broken=True,
            unfree=True,
        ),
    ]


def test_the_cache_round_trips_every_field(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    records = _some_records()
    save_cache(path, "/nix/store/abc-source", records)
    assert load_cache(path) == records


def test_the_cache_is_named_for_the_nixpkgs_it_holds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two pins are two files, which is why a hit is exactly right."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    first = cache_path("/nix/store/aaaa-source")
    second = cache_path("/nix/store/bbbb-source")
    assert first != second
    assert first.name == "aaaa-source.json"


def test_a_missing_cache_is_not_an_error(tmp_path: Path) -> None:
    assert load_cache(tmp_path / "nothing.json") is None


def test_a_truncated_cache_is_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    path.write_text('{"version": 1, "packages": [')
    assert load_cache(path) is None


def test_a_cache_of_another_version_is_ignored(tmp_path: Path) -> None:
    """A record that gains a field must not be read into the old shape."""
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"version": 0, "packages": []}))
    assert load_cache(path) is None


def test_a_cache_that_is_not_an_object_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    path.write_text(json.dumps(["not", "a", "cache"]))
    assert load_cache(path) is None


def test_the_write_leaves_no_partial_file(tmp_path: Path) -> None:
    """Two `pynix` processes can index at once, and a reader must see one file.

    The write goes to a neighbour and is renamed, which is atomic inside one
    directory.
    """
    path = tmp_path / "index.json"
    save_cache(path, "/nix/store/abc-source", _some_records())
    assert [entry.name for entry in tmp_path.iterdir()] == ["index.json"]


def test_the_cache_is_overwritten_whole(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    save_cache(path, "/nix/store/abc-source", _some_records())
    save_cache(path, "/nix/store/abc-source", _some_records()[:1])
    cached = load_cache(path)
    assert cached is not None
    assert len(cached) == 1


async def test_a_second_index_reads_the_cache(
    shared_nix_environment: NixTestEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The walk runs once, and the second call answers from disk.

    Measured on real nixpkgs: 12.7 s for the walk and 0.10 s for the cache.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    target = EvaluationTarget(file=str(_PKGSET), attr=None, flake=None)
    async with eval_session(shared_nix_environment.store_uri) as (_nix, _store, session):
        root = await evaluate_target(target, session, auto_call_file=True)
        lib_value = await select_attr(root, "lib")
        first = await indexed_packages(session, root, lib_value)
        identity = await package_identity(session, root)
        assert cache_path(identity).is_file()

        # Corrupting the walk would change a fresh result and not a cached one.
        second = await indexed_packages(session, root, lib_value)
        assert second == first

        refreshed = await indexed_packages(session, root, lib_value, refresh=True)
        assert refreshed == first


#: Two package sets that differ in one config value and in nothing else. The
#: fixture inherits `path` from real nixpkgs, so both carry the same source
#: and the old key -- the store path alone -- gave them the same file.
_ONE_CONFIG_APART = """
let
  base = import {fixture} {{ }};
in {{
  free = base // {{ config = {{ allowUnfree = false; }}; }};
  unfree = base // {{ config = {{ allowUnfree = true; }}; }};
}}
"""


async def test_two_package_sets_that_differ_only_in_config_get_different_keys(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """Regression test for issue #260.

    `pkgs.path` was the whole key, and `import <nixpkgs> {{ }}` and
    `import <nixpkgs> {{ config.allowUnfree = true; }}` have the same one. The
    second therefore read the walk of the first, silently.
    """
    expression = _ONE_CONFIG_APART.format(fixture=_PKGSET)
    async with eval_session(shared_nix_environment.store_uri) as (_nix, _store, session):
        both = await session.string(expression)
        free = await package_identity(session, await select_attr(both, "free"))
        unfree = await package_identity(session, await select_attr(both, "unfree"))

    assert free != unfree, f"one config value apart, and the same key: {free}"
    # The source still names the file, so a reader can still see which nixpkgs
    # a cache file belongs to.
    assert free.split("-")[0] == unfree.split("-")[0]


async def test_the_same_package_set_keeps_one_key(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """The key has to be stable, or every search would walk again."""
    target = EvaluationTarget(file=str(_PKGSET), attr=None, flake=None)
    async with eval_session(shared_nix_environment.store_uri) as (_nix, _store, session):
        first = await package_identity(session, await evaluate_target(target, session, auto_call_file=True))
        second = await package_identity(session, await evaluate_target(target, session, auto_call_file=True))

    assert first == second


async def test_a_config_holding_a_function_still_answers(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """`builtins.toJSON` raises on a function, and a real config holds one.

    `allowUnfreePredicate` is the entry that made `pkgs.config` unusable as a
    key. The facts mark it by its type instead of its value, so the key is
    built rather than the evaluation failing.
    """
    expression = f"""
    let base = import {_PKGSET} {{ }};
    in base // {{ config = {{ allowUnfreePredicate = _: true; }}; }}
    """
    async with eval_session(shared_nix_environment.store_uri) as (_nix, _store, session):
        value = await session.string(expression)
        identity = await package_identity(session, value)

    assert identity
