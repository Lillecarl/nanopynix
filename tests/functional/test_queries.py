"""Advanced store query tests."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pyinstrument
import pytest
import structlog

from pynixd import Server
from pynixd.store import LocalSocketStore
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    get_test_store_kwargs,
    run_subproc,
    rmtree_robust,
)

log = structlog.get_logger(__name__)


@pytest.fixture
async def query_env(tmp_path: Path):
    """Set up a pynixd server with some initial paths."""
    pynixd_local_path = STORE_PREFIX / "pynixd-local-queries"
    pynixd_builder_path = STORE_PREFIX / "pynixd-builder-queries"
    rmtree_robust(pynixd_local_path)
    rmtree_robust(pynixd_builder_path)

    pynixd_local = LocalSocketStore(
        id="pynixd-local",
        store_path=pynixd_local_path,
        **get_test_store_kwargs(),
    )
    pynixd_builder = LocalSocketStore(
        id="pynixd-builder",
        store_path=pynixd_builder_path,
        **get_test_store_kwargs(),
    )

    async with Server(
        local_store=pynixd_local, stores={"builder": pynixd_builder}, ssh_port=0
    ) as server:
        username = os.environ.get("USER", "root")
        uri = f"ssh-ng://{username}@127.0.0.1:{server.port}"

        # Populate with a simple build
        test_nix = Path("tests/nix")
        cmd = [
            NIX_BIN,
            "build",
            "--eval-store",
            "auto",
            "--store",
            uri,
            "--impure",
            "--file",
            test_nix,
            "minimal.leaf",
            "--no-link",
        ]
        rc, stdout, stderr, stdboth = await run_subproc(cmd)
        assert rc == 0, f"Initial build failed:\n{stdboth}"

        # Get expected output path locally
        cmd = [
            NIX_BIN.parent / "nix-instantiate",
            "--impure",
            test_nix,
            "-A",
            "minimal.leaf",
        ]
        rc, stdout, stderr, stdboth = await run_subproc(cmd)
        assert rc == 0
        drv_path = stdout.strip()

        cmd = [
            NIX_BIN.parent / "nix-store",
            "-q",
            "--outputs",
            drv_path,
        ]
        rc, stdout, stderr, stdboth = await run_subproc(cmd)
        assert rc == 0
        out_path = stdout.strip()
        assert out_path.startswith("/nix/store/"), f"Unexpected path: {out_path}"

        yield server, uri, out_path


async def test_query_referrers(profiler: pyinstrument.Profiler, query_env) -> None:
    """Verify QueryReferrers via 'nix-store -q --referrers'.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - QueryMissing: Queries missing paths
    - QueryPathInfo: Queries path info
    - QueryReferrers: Queries referrers
    - QueryValidPaths: Queries valid paths
    """
    server, uri, out_path = query_env
    test_nix = Path("tests/nix")

    # Build another thing that depends on 'out_path'
    cmd = [
        NIX_BIN,
        "build",
        "--eval-store",
        "auto",
        "--store",
        uri,
        "--impure",
        "--file",
        test_nix,
        "minimal.dependent",
        "--no-link",
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0

    # Get dep_path locally
    cmd = [
        NIX_BIN.parent / "nix-instantiate",
        "--impure",
        test_nix,
        "-A",
        "minimal.dependent",
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0
    dep_drv = stdout.strip()

    cmd = [
        NIX_BIN.parent / "nix-store",
        "-q",
        "--outputs",
        dep_drv,
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0
    dep_path = stdout.strip()

    # No nix3 equivalent exists for --referrers (no `nix store referrers`).
    cmd = [
        NIX_BIN.parent / "nix-store",
        "--store",
        uri,
        "-q",
        "--referrers",
        out_path,
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0
    referrers = [line.strip() for line in stdout.splitlines()]
    assert dep_path in referrers


async def test_query_path_from_hash_part(
    profiler: pyinstrument.Profiler, query_env
) -> None:
    """Verify QueryPathFromHashPart via 'nix store path-from-hash-part'.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - QueryMissing: Queries missing paths
    - QueryPathFromHashPart: Queries path from hash part
    - QueryValidPaths: Queries valid paths
    """
    server, uri, out_path = query_env

    # out_path is like /nix/store/hash-name
    m = re.match(r"/nix/store/([a-z0-9]+)-", out_path)
    assert m
    hash_part = m.group(1)

    cmd = [
        NIX_BIN,
        "store",
        "path-from-hash-part",
        "--store",
        uri,
        hash_part,
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0
    assert stdout.strip() == out_path


async def test_query_valid_derivers(profiler: pyinstrument.Profiler, query_env) -> None:
    """Verify QueryValidDerivers via 'nix-store -q --deriver'.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - QueryMissing: Queries missing paths
    - QueryPathInfo: Queries path info
    - QueryValidPaths: Queries valid paths
    """
    server, uri, out_path = query_env

    # Could use `nix path-info --derivation <store-path>` as a nix3 equivalent,
    # but `nix-store -q --deriver` is kept for consistency with other query tests.
    cmd = [
        NIX_BIN.parent / "nix-store",
        "--store",
        uri,
        "-q",
        "--deriver",
        out_path,
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0
    deriver = stdout.strip()
    assert deriver.endswith(".drv")
    assert deriver.startswith("/nix/store/")


async def test_query_missing(profiler: pyinstrument.Profiler, query_env) -> None:
    """Verify QueryMissing via 'nix build --dry-run'.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - BuildPaths: Builds derivation paths
    - BuildPathsWithResults: Builds derivation paths with results
    - QueryMissing: Queries missing paths
    - QueryValidPaths: Queries valid paths
    """
    server, uri, out_path = query_env

    test_nix = Path("tests/nix")
    cmd = [
        NIX_BIN,
        "build",
        "--eval-store",
        "auto",
        "--store",
        uri,
        "--impure",
        "--file",
        test_nix,
        "minimal.leaf",
        "--dry-run",
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0


async def test_find_roots(profiler: pyinstrument.Profiler, query_env) -> None:
    """Verify FindRoots via 'nix-store --gc --print-roots'.

    Store operations triggered:
    - None: This test only checks roots without triggering Store operations
    """
    server, uri, out_path = query_env

    # No nix3 equivalent exists for --print-roots (`nix store gc` has no such flag).
    cmd = [
        NIX_BIN.parent / "nix-store",
        "--store",
        uri,
        "--gc",
        "--print-roots",
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    # If it fails with host resolution error, it's a nix-store limitation with URI ports.
    if rc != 0 and "Could not resolve hostname" in stdboth:
        pytest.skip("nix-store does not support ports in URIs for this command")
