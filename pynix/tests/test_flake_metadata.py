"""Does ``pynix flake metadata`` print what ``nix flake metadata --json`` prints?

The whole object, and not the ``locks`` key alone. That is the acceptance test
for issue #79, and it is worth stating why it is an oracle test rather than a
list of assertions about a record format: a restated format would agree with a
wrong implementation as easily as with a right one, because the same reading of
Nix produces both.

Nix builds the object in ``CmdFlakeMetadata::run``, from one ``LockedFlake``
and one ``Store``. The binding behind ``metadata_json`` copies those lines, so
nothing here or in Python assembles it. What the command used to print instead
was a bespoke object with ``resolvedRef`` and an ``inputs`` map filled from the
inputs the ``flake.nix`` *declared*: the original reference under a name that
said locked, with no ``rev`` and nowhere to put a transitive node.

The fixture is three local git flakes with a transitive input and a ``follows``
edge, so the comparison runs offline and covers the shapes a flat map cannot
express.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING, Any

import pytest

from pynix import Pynix
from test_support.git_fixtures import init_linked_flakes
from test_support.subprocess_output import run_process

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix_testing.nix_environment import NixTestEnvironment


async def _nix_flake_metadata(flake: Path, store_uri: str) -> dict[str, Any]:
    """The oracle: what ``nix flake metadata --json`` says about the same flake."""
    if shutil.which("nix") is None:
        pytest.skip("the nix CLI is the oracle for this test, and it is not on PATH")
    result = await run_process(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "flake",
            "metadata",
            "--json",
            # `pynix flake metadata` passes write_lock_file=False, so the oracle
            # must not write one either. Otherwise whichever command ran first
            # would leave a lock file and the second would read it.
            "--no-write-lock-file",
            "--store",
            store_uri,
            str(flake),
        ],
    )
    if result.returncode != 0:
        pytest.fail(f"the oracle failed: nix flake metadata {result.describe()}")
    return json.loads(result.stdout)


async def _pynix_flake_metadata(
    flake: Path,
    environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    cmd = Pynix.parse(["flake", "metadata", str(flake), *environment.pynix_store_args()])
    await cmd.astart()
    return json.loads(capsys.readouterr().out)


async def test_flake_metadata_matches_nix(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Every key, for a flake whose lock file is a graph."""
    flakes = init_linked_flakes(tmp_path)
    theirs = await _nix_flake_metadata(flakes.root, shared_nix_environment.store_uri)
    mine = await _pynix_flake_metadata(flakes.root, shared_nix_environment, capsys)

    assert mine == theirs


async def test_the_locks_object_holds_the_whole_graph(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The equality above would also pass on two identically empty graphs.

    So this names what the graph has to contain. Without it, a ``locks`` that
    Nix and pynix both reported as ``{}`` would look like agreement.
    """
    flakes = init_linked_flakes(tmp_path)
    mine = await _pynix_flake_metadata(flakes.root, shared_nix_environment, capsys)

    locks = mine["locks"]
    nodes = locks["nodes"]
    root = nodes[locks["root"]]
    assert set(root["inputs"]) == {"leaf", "mid"}

    # A `follows` edge is a path into the graph; a node edge is that node's key.
    assert nodes[root["inputs"]["mid"]]["inputs"]["leaf"] == ["leaf"]

    # And the locked node carries what the declared map never did.
    assert "rev" in nodes[root["inputs"]["leaf"]]["locked"]
