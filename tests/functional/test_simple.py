"""Simple end-to-end build tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pyinstrument
import pytest
import structlog

from pynixd import Server
from pynixd.store import get_current_system
from tests.conftest import (
    NIX_BIN,
    run_subproc,
    set_log_levels,
)

log = structlog.get_logger(__name__)


@pytest.mark.timeout(60)
async def test_builders(
    profiler: pyinstrument.Profiler, pynixd_server: Server, tmp_path: Path
) -> None:
    """Build nix/standard.simple via --builders.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - QueryMissing: Queries missing paths
    - QueryValidPaths: Queries valid paths
    """
    test_nix = Path("tests/nix")
    client_store_path = tmp_path / "client-store"

    uri = pynixd_server.uri()
    system = get_current_system()
    builder_spec = f"{uri} {system}"

    cmd = [
        str(NIX_BIN),
        "build",
        "--store",
        str(client_store_path),
        "--builders",
        builder_spec,
        "--file",
        str(test_nix),
        "simple",
        "--no-link",
        "--print-out-paths",
        "--max-jobs",
        "0",
    ]
    rc, stdout, stderr, stdboth = await run_subproc(
        cmd, env={"NIX_STATE_DIR": str(client_store_path / "var/nix")}
    )
    assert rc == 0, f"build failed:\n{stdboth}"


@pytest.mark.timeout(60)
async def test_store(
    profiler: pyinstrument.Profiler, pynixd_server: Server, tmp_path: Path
) -> None:
    """Build nix/standard.simple via --store.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - QueryMissing: Queries missing paths
    - QueryValidPaths: Queries valid paths
    """
    test_nix = Path("tests/nix")

    with set_log_levels({"pynixd.op.AddToStore": logging.INFO}):
        uri = pynixd_server.uri()

        cmd = [
            str(NIX_BIN),
            "build",
            "--eval-store",
            "auto",
            "--store",
            uri,
            "--file",
            str(test_nix),
            "simple",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr, stdboth = await run_subproc(cmd)
        assert rc == 0, f"build failed:\n{stdboth}"
