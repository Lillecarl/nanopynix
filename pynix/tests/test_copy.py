"""``pynix copy`` between two stores that this test owns.

Two derivations, where the top names the leaf. Nix scans each output for the
store paths it holds, so the closure of the top is exactly those two paths and
nothing that a substituter would have to supply. The destination is a second
chroot store under a directory of pytest, so the whole suite needs no network
and leaves the store of the machine alone. Issue #80.
"""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING

import pytest

from nanopynix import stores
from nanopynix.exceptions import NixError
from nanopynix_testing.nix_environment import force_rmtree, with_nixpkgs
from nanopynix_testing.nix_markers import LINUX_CHROOT_BUILD
from pynix import parse
from test_support.subprocess_output import run_process

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

    **``require-sigs`` is in the URI, and it is not left to the default.**
    ``test_an_unsigned_path_needs_no_check_sigs`` needs a destination that
    refuses an unsigned path, and a store reads its settings once, when it is
    constructed --
    ``nanopynix/tests/test_config_flow.py::test_a_store_setting_in_the_uri_beats_the_global``
    measured that, and that the URI beats the global. So the fixture states
    the precondition rather than borrowing whatever the process holds.

    This is not what made that case fail in CI, and the note is here so that
    nobody reads it as the cure. The cause was ``keep-going``, left on in the
    pytest process by another test: Nix then ended the copy quietly, wrote
    nothing, and raised nothing.
    """
    root = tmp_path_factory.mktemp("pynix-copy-destination")
    try:
        yield stores.Local(root=str(root), require_sigs=True).uri()
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
    go on. The destination carries ``require-sigs=true`` in its URI, so it
    refuses -- see :func:`destination_store` for why that is not left to the
    process-wide default.
    """
    signed = [built_closure["top"], "--to", destination_store, *shared_nix_environment.pynix_store_args()]

    # `match`, so the case cannot pass on some other failure of Nix.
    with pytest.raises(NixError, match="signature"):
        await _copy(signed, capsys)

    result = await _copy([*signed, "--no-check-sigs"], capsys)

    assert result["copied"] == sorted([built_closure["top"], built_closure["leaf"]])


#: A child that turns the process-wide ``keep-going`` on and then copies.
#:
#: **A child, and not this process.** ``pynix/tests/_shared_sessions.py``
#: patches ``pynix._util.nix_session`` so the whole suite reuses one inproc
#: session with its stores open, and ``set_settings`` is refused while a store
#: is open. A child has neither, so it is the only place this setting can be
#: applied to the session that the command itself opens.
_QUIET_COPY_PROBE = """
import json
import sys

import anyio

import nanopynix
from pynix import parse
from test_support.subprocess_output import run_process

source, path, destination = sys.argv[1], sys.argv[2], sys.argv[3]


async def main() -> None:
    async with nanopynix.inproc.Session() as session:
        await session.set_settings(nanopynix.NixGlobalSettings(keep_going=True))
    command = parse(["copy", path, "--to", destination, "--store", source])
    try:
        await command.run()
    except SystemExit as exit_request:
        print(json.dumps({"exit": exit_request.code}))
        return
    print(json.dumps({"exit": 0}))


anyio.run(main)
"""


@LINUX_CHROOT_BUILD
async def test_a_copy_that_writes_nothing_is_not_reported_as_a_copy(
    shared_nix_environment: NixTestEnvironment,
    built_closure: dict[str, str],
    destination_store: str,
) -> None:
    """Nix can end a copy quietly, and the command must not believe it.

    **Measured.** With the process-wide ``keep-going`` on, copying an unsigned
    path into a store that requires a signature raises nothing and writes
    nothing. Before the check this test pins, ``pynix copy`` reported both
    paths as copied, because its report was the difference between the two
    stores computed *before* the copy -- what it meant to copy, and not what
    it did. The destination held neither path afterwards.

    That is how ``test_an_unsigned_path_needs_no_check_sigs`` came to fail in
    every full-suite job of CI and to pass whenever it ran alone:
    ``nanopynix/tests/test_config_flow.py`` wrote ``keep-going`` into the
    pytest process and left it there. That test puts it back now, and this one
    states what the command does when a copy is quiet, whatever the reason.
    """
    result = await run_process(
        [
            sys.executable,
            "-c",
            _QUIET_COPY_PROBE,
            shared_nix_environment.store_uri,
            built_closure["top"],
            destination_store,
        ],
    )

    assert json.loads(result.stdout.strip().splitlines()[-1])["exit"] == 1, result.describe()
    # `error_console` wraps for a terminal, and the wrap point moves with the
    # width and with the length of each store path. `test_why_depends.py`
    # carries the full account of why the match ignores every run of space.
    assert "didnotreach" in re.sub(r"\s+", "", result.stderr), result.describe()


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
