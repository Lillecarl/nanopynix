"""Tests for the bulk package walk that `pynix search` reads.

The fixture is a real attribute set: `lib` and `pkgs` come from this
repository, and each package in it is a real derivation. So the walk under test
runs against the thing it will meet, and not against a double.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pynix._packages import PackageRecord, fetch_package_list
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
