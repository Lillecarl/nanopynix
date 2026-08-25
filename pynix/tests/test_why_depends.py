"""``pynix why-depends`` over a chain whose shape the test builds itself.

Three derivations, where the top names the middle and the middle names the
leaf. Nix scans each output for the store paths it holds, so the references it
records give the chain ``top -> middle -> leaf`` and nothing shorter. Issue #83.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest

from nanopynix_testing.nix_environment import with_nixpkgs
from nanopynix_testing.nix_markers import LINUX_CHROOT_BUILD
from pynix import parse
from support.nix_oracle import require_matching_nix_cli
from test_support.subprocess_output import run_process

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix_testing.nix_environment import NixTestEnvironment

# Each output holds the store path of the one below it, and nothing else. A
# `runCommand` with no build inputs keeps the closure to these three paths, so
# the chain the command reports is the chain this expression writes.
_CHAIN_EXPRESSION = """with import <nixpkgs> {};
let
  leaf = runCommand "why-depends-leaf" {} "printf '%s' why-depends-leaf > $out";
  middle = runCommand "why-depends-middle" {} "printf '%s' ${leaf} > $out";
  top = runCommand "why-depends-top" {} "printf '%s' ${middle} > $out";
in { inherit leaf middle top; }
"""


async def _build(
    environment: NixTestEnvironment,
    nix_file: Path,
    attribute: str,
    capsys: pytest.CaptureFixture[str],
) -> str:
    cmd = parse(["build", "--file", str(nix_file), "--attr", attribute, *environment.pynix_store_args()])
    await cmd.run()
    outputs = json.loads(capsys.readouterr().out)["outputs"]
    return str(outputs["out"])


@pytest.fixture
async def reference_chain(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, str]:
    """The three built paths, by attribute name.

    ``top`` builds the other two as well, so the two builds after it are
    evaluations against a store that already holds the result.
    """
    nix_file = tmp_path / "chain.nix"
    nix_file.write_text(with_nixpkgs(_CHAIN_EXPRESSION, nixpkgs_path))
    return {
        attribute: await _build(shared_nix_environment, nix_file, attribute, capsys)
        for attribute in ("top", "middle", "leaf")
    }


@LINUX_CHROOT_BUILD
async def test_a_direct_reference_gives_a_chain_of_two_nodes(
    shared_nix_environment: NixTestEnvironment,
    reference_chain: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        ["why-depends", reference_chain["top"], reference_chain["middle"], *shared_nix_environment.pynix_store_args()],
    )

    await cmd.run()

    result = json.loads(capsys.readouterr().out)
    assert result["chain"] == [reference_chain["top"], reference_chain["middle"]]
    assert result["package"] == reference_chain["top"]
    assert result["dependency"] == reference_chain["middle"]


@LINUX_CHROOT_BUILD
async def test_a_transitive_reference_gives_the_whole_chain(
    shared_nix_environment: NixTestEnvironment,
    reference_chain: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        ["why-depends", reference_chain["top"], reference_chain["leaf"], *shared_nix_environment.pynix_store_args()],
    )

    await cmd.run()

    result = json.loads(capsys.readouterr().out)
    assert result["chain"] == [reference_chain["top"], reference_chain["middle"], reference_chain["leaf"]]


@LINUX_CHROOT_BUILD
async def test_a_path_depends_on_itself_through_a_chain_of_one_node(
    shared_nix_environment: NixTestEnvironment,
    reference_chain: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What ``nix why-depends`` answers for the same pair. Its own help gives
    ``nix why-depends nixpkgs#glibc nixpkgs#glibc`` as an example, and the one
    path is the whole output."""
    cmd = parse(
        ["why-depends", reference_chain["leaf"], reference_chain["leaf"], *shared_nix_environment.pynix_store_args()],
    )

    await cmd.run()

    assert json.loads(capsys.readouterr().out)["chain"] == [reference_chain["leaf"]]


@LINUX_CHROOT_BUILD
async def test_two_unrelated_paths_report_no_chain_and_exit_non_zero(
    shared_nix_environment: NixTestEnvironment,
    reference_chain: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The leaf refers to nothing, so the top is not in its closure."""
    cmd = parse(
        ["why-depends", reference_chain["leaf"], reference_chain["top"], *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit) as exit_info:
        await cmd.run()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    # stdout stays empty: the output of this command is JSON, and a caller
    # writes `pynix why-depends ... | jq`.
    assert captured.out == ""
    # `error_console` wraps for a terminal, and the wrap point moves with the
    # width and with the length of each store path, so it lands inside the
    # message under one root and not under another. Match the text with every
    # run of whitespace removed: the rendering then cannot decide whether the
    # assertion holds.
    flattened = re.sub(r"\s+", "", captured.err)
    assert "doesnotdependon" in flattened
    assert reference_chain["leaf"] in flattened
    assert reference_chain["top"] in flattened


@LINUX_CHROOT_BUILD
async def test_the_chain_agrees_with_nix_why_depends(
    shared_nix_environment: NixTestEnvironment,
    reference_chain: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every node of the chain is one that Nix names, in the same order.

    `nix why-depends` prints a tree and marks each edge, so the comparison
    reads the order of the store paths in that output rather than its shape.
    """
    await require_matching_nix_cli()
    result = await run_process(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "--store",
            shared_nix_environment.store_uri,
            "why-depends",
            reference_chain["top"],
            reference_chain["leaf"],
        ],
    )
    assert result.returncode == 0, result.describe()

    cmd = parse(
        ["why-depends", reference_chain["top"], reference_chain["leaf"], *shared_nix_environment.pynix_store_args()],
    )
    await cmd.run()
    chain = json.loads(capsys.readouterr().out)["chain"]

    reported = result.stdout
    positions = [reported.find(path) for path in chain]
    assert all(position >= 0 for position in positions), reported
    assert positions == sorted(positions), reported


async def test_why_depends_reports_an_invalid_path(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        [
            "why-depends",
            "/nix/store/deadbeef-nonexistent",
            "/nix/store/deadbeef-also-nonexistent",
            *shared_nix_environment.pynix_store_args(),
        ],
    )

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error" in captured.err
