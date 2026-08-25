"""``pynix copy`` between two stores that this test owns.

Two derivations, where the top names the leaf. Nix scans each output for the
store paths it holds, so the closure of the top is exactly those two paths and
nothing that a substituter would have to supply. The destination is a second
chroot store under a directory of pytest, so the whole suite needs no network
and leaves the store of the machine alone. Issue #80.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from nanopynix.exceptions import NixError
from nanopynix_testing.nix_environment import force_rmtree, with_nixpkgs
from nanopynix_testing.nix_markers import LINUX_CHROOT_BUILD
from pynix import parse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nanopynix_testing.nix_environment import NixTestEnvironment

# `runCommand` with no build inputs, so each output holds the store path below
# it and nothing else. The closure of `top` is `{top, leaf}`.
_CLOSURE_EXPRESSION = """with import <nixpkgs> {};
let
  leaf = runCommand "pynix-copy-leaf" {} "printf '%s' pynix-copy-leaf > $out";
  top = runCommand "pynix-copy-top" {} "printf '%s' ${leaf} > $out";
in { inherit leaf top; }
"""


@pytest.fixture
async def destination_store(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[str]:
    """A second chroot store, empty, and its own for each test.

    Not under a test's ``tmp_path``: ``isolated_nix_environment`` gives the
    reason, and it holds here as well -- a test that evaluates its ``tmp_path``
    as a path flake would reach into this store.

    Function scope, and not session scope. Two of these tests copy the same
    closure, and one of them measures a store that has never seen it.
    """
    root = tmp_path_factory.mktemp("pynix-copy-destination")
    try:
        yield f"local://?root={root}"
    finally:
        await force_rmtree(root)


@pytest.fixture
def chain_file(nixpkgs_path: str, tmp_path: Path) -> Path:
    written = tmp_path / "closure.nix"
    written.write_text(with_nixpkgs(_CLOSURE_EXPRESSION, nixpkgs_path))
    return written


@pytest.fixture
async def built_closure(
    shared_nix_environment: NixTestEnvironment,
    chain_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, str]:
    """The two built paths, by attribute name.

    ``top`` builds the leaf as well, so the second build is an evaluation
    against a store that already holds the result.
    """
    built: dict[str, str] = {}
    for attribute in ("top", "leaf"):
        command = parse(
            ["build", "--file", str(chain_file), "--attr", attribute, *shared_nix_environment.pynix_store_args()],
        )
        await command.run()
        built[attribute] = str(json.loads(capsys.readouterr().out)["outputs"]["out"])
    return built


async def _copy(
    arguments: list[str],
    capsys: pytest.CaptureFixture[str],
) -> dict[str, object]:
    command = parse(["copy", *arguments])
    await command.run()
    return json.loads(capsys.readouterr().out)


@LINUX_CHROOT_BUILD
async def test_a_copy_carries_the_whole_closure_into_the_second_store(
    shared_nix_environment: NixTestEnvironment,
    built_closure: dict[str, str],
    destination_store: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The caller names one path, and the leaf it refers to arrives with it."""
    result = await _copy(
        [
            built_closure["top"],
            "--to",
            destination_store,
            # The paths were built here a moment ago and nothing signed them.
            # `test_an_unsigned_path_needs_no_check_sigs` is what states that.
            "--no-check-sigs",
            *shared_nix_environment.pynix_store_args(),
        ],
        capsys,
    )

    assert result["requested"] == [built_closure["top"]]
    assert result["copied"] == sorted([built_closure["top"], built_closure["leaf"]])
    assert result["alreadyPresent"] == []
    assert result["to"] == destination_store

    # The destination now answers for both paths, which is the copy having
    # happened rather than the command having said so.
    for path in built_closure.values():
        command = parse(["store", "is-valid-path", path, "--store", destination_store])
        await command.run()
        assert json.loads(capsys.readouterr().out)["valid"] is True


@LINUX_CHROOT_BUILD
async def test_a_closure_that_is_present_already_copies_nothing(
    shared_nix_environment: NixTestEnvironment,
    built_closure: dict[str, str],
    destination_store: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [
        built_closure["top"],
        "--to",
        destination_store,
        "--no-check-sigs",
        *shared_nix_environment.pynix_store_args(),
    ]
    await _copy(arguments, capsys)

    second = await _copy(arguments, capsys)

    assert second["copied"] == []
    assert second["alreadyPresent"] == sorted([built_closure["top"], built_closure["leaf"]])


@LINUX_CHROOT_BUILD
async def test_an_unsigned_path_needs_no_check_sigs(
    shared_nix_environment: NixTestEnvironment,
    built_closure: dict[str, str],
    destination_store: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default refuses a path that nothing signed, and the flag allows it.

    A path built in the source store is ``ultimate`` there, and a copy drops
    that mark: the destination did not build it and has only the signatures to
    go on. ``require-sigs`` is on by default, so the destination refuses.
    """
    signed = [built_closure["top"], "--to", destination_store, *shared_nix_environment.pynix_store_args()]

    with pytest.raises(NixError) as error_info:
        await _copy(signed, capsys)
    # The message, so that the case cannot pass on some other failure of Nix.
    assert "signature" in str(error_info.value)

    result = await _copy([*signed, "--no-check-sigs"], capsys)

    assert result["copied"] == sorted([built_closure["top"], built_closure["leaf"]])


@LINUX_CHROOT_BUILD
async def test_a_file_and_an_attr_name_the_paths_to_copy(
    shared_nix_environment: NixTestEnvironment,
    built_closure: dict[str, str],
    chain_file: Path,
    destination_store: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same copy, with the target named the way ``pynix build`` names it."""
    result = await _copy(
        [
            "--file",
            str(chain_file),
            "--attr",
            "top",
            "--to",
            destination_store,
            "--no-check-sigs",
            *shared_nix_environment.pynix_store_args(),
        ],
        capsys,
    )

    assert result["requested"] == [built_closure["top"]]
    assert result["copied"] == sorted([built_closure["top"], built_closure["leaf"]])


@LINUX_CHROOT_BUILD
async def test_from_pulls_out_of_the_store_it_names(
    shared_nix_environment: NixTestEnvironment,
    built_closure: dict[str, str],
    destination_store: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--from`` names the source, and ``--store`` is then the destination."""
    result = await _copy(
        [
            built_closure["leaf"],
            "--from",
            shared_nix_environment.store_uri,
            "--store",
            destination_store,
            "--no-check-sigs",
        ],
        capsys,
    )

    assert result["from"] == shared_nix_environment.store_uri
    assert result["to"] == destination_store
    assert result["copied"] == [built_closure["leaf"]]


async def test_a_copy_with_no_second_store_reports_what_is_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = parse(["copy", "/nix/store/deadbeef-nonexistent"])

    with pytest.raises(SystemExit) as exit_info:
        await command.run()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    # stdout stays empty: the output of this command is JSON, and a caller
    # writes `pynix copy ... | jq`.
    assert captured.out == ""
    assert "--to" in captured.err


async def test_the_same_store_on_both_sides_is_refused(
    destination_store: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = parse(
        ["copy", "/nix/store/deadbeef-nonexistent", "--to", destination_store, "--store", destination_store]
    )

    with pytest.raises(SystemExit):
        await command.run()

    assert "same store" in capsys.readouterr().err


async def test_a_copy_that_names_no_path_at_all_is_refused(
    shared_nix_environment: NixTestEnvironment,
    destination_store: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = parse(["copy", "--to", destination_store, *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await command.run()

    assert "--file" in capsys.readouterr().err
