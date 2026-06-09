"""Simple end-to-end build tests."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
import structlog

from pynixd.store import get_current_system
from tests.conftest import (
    CLIENT_BIN,
    TEST_NIX,
    run_subproc,
    server_uri,
    set_log_levels,
)
from tests.test_features import TestFeatures as F

if TYPE_CHECKING:
    from pathlib import Path

    import pyinstrument

    from pynixd import Server

log = structlog.get_logger(__name__)


@pytest.mark.covers(
    F.REGULAR
    | F.TEXT_OUTPUT
    | F.BUILD_DERIVATION
    | F.BUILD_PATHS
    | F.BUILD_PATHS_WITH_RESULTS
    | F.GOAL_BUILD
    | F.GOAL_DAG
    | F.GOAL_BUILD_QUEUE
    | F.GOAL_SCHEDULER
    | F.STORE_SSH
    | F.STORE_REVERSE
)
@pytest.mark.timeout(60)
async def test_builders(
    profiler: pyinstrument.Profiler,
    pynixd_server: Server,
    tmp_path: Path,
) -> None:
    """Build nix/standard.simple via --builders.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - QueryMissing: Queries missing paths
    - QueryValidPaths: Queries valid paths
    """
    test_nix = TEST_NIX
    client_store_path = tmp_path / "client-store"

    uri = server_uri(pynixd_server)
    system = get_current_system()
    builder_spec = f"{uri} {system}"

    cmd = [
        str(CLIENT_BIN),
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
        cmd,
        env={"NIX_STATE_DIR": str(client_store_path / "var/nix")},
    )
    assert rc == 0, f"build failed:\n{stdboth}"
    out_path = stdout.strip()
    assert out_path.startswith("/nix/store/"), f"Expected store path, got: {out_path}"


@pytest.mark.covers(
    F.REGULAR
    | F.TEXT_OUTPUT
    | F.BUILD_DERIVATION
    | F.BUILD_PATHS
    | F.BUILD_PATHS_WITH_RESULTS
    | F.GOAL_BUILD
    | F.GOAL_DAG
    | F.GOAL_BUILD_QUEUE
    | F.GOAL_SCHEDULER
    | F.STORE_LOCAL
)
@pytest.mark.timeout(60)
async def test_store(
    profiler: pyinstrument.Profiler,
    pynixd_server: Server,
    tmp_path: Path,
) -> None:
    """Build nix/standard.simple via --store."""
    test_nix = TEST_NIX

    with set_log_levels({"pynixd.op.AddToStore": logging.INFO}):
        uri = server_uri(pynixd_server)

        cmd = [
            str(CLIENT_BIN),
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
        out_path = stdout.strip()
        assert out_path.startswith("/nix/store/"), f"Expected store path, got: {out_path}"
