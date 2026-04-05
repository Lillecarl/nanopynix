"""Tests for "pynixd local mode" (no sub-builders configured)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import structlog
from conftest import (
    LIX_BIN,
    NIX_BIN,
    TEST_NIX,
    get_free_port,
    nix_command,
    rmtree_robust,
)

from pynixd.instance import NixImplementation, PynixdConfig, Server
from pynixd.store import LocalSocketStore

log = structlog.get_logger(__name__)


@pytest.mark.asyncio
async def test_local_mode_passthrough(nix_env):
    """Verify that pynixd correctly passes builds through to the local daemon
    in local mode."""
    local_store_path = Path("/tmp/pynixd-test-local-mode")
    rmtree_robust(local_store_path)
    os.makedirs(local_store_path, exist_ok=True)

    unix_socket = local_store_path / "pynixd.sock"

    # Start pynixd with NO builders
    local_store = LocalSocketStore(
        store_path=local_store_path,
        id="local",
    )

    config = PynixdConfig(
        local_store=local_store,
        stores={},  # Explicitly empty
        unix_path=unix_socket,
    )

    async with Server(config):
        # Build a simple derivation through pynixd
        # It should pass through to the local daemon managed by pynixd
        rc, stdout, stderr = await (
            nix_command(LIX_BIN)
            .remote(f"unix://{unix_socket}")
            .file(TEST_NIX, "simple")
            .with_env(nix_env)
            .run()
        )
        log.debug("build_result", rc=rc, stdout=stdout, stderr=stderr)

        assert rc == 0, f"Local mode build failed:\n{stderr}"
        assert "test-" in stdout

        # Verify the output exists in our local store path on the host
        out_path_relative = stdout.strip()
        # If out_path_relative is /nix/store/..., the host path is
        # local_store_path / "nix/store/..."
        out_path_host = local_store_path / out_path_relative.lstrip("/")
        assert os.path.exists(out_path_host)


@pytest.mark.asyncio
async def test_local_mode_is_valid_path(nix_env):
    """Verify that IsValidPath still works in local mode."""
    local_store_path = Path("/tmp/pynixd-test-local-mode-query")
    rmtree_robust(local_store_path)
    os.makedirs(local_store_path, exist_ok=True)

    unix_socket = local_store_path / "pynixd.sock"

    local_store = LocalSocketStore(
        store_path=local_store_path,
        id="local",
    )

    config = PynixdConfig(
        local_store=local_store,
        stores={},
        unix_path=unix_socket,
    )

    async with Server(config):
        # 1. Build something to get a valid path
        rc, stdout, _ = await (
            nix_command(LIX_BIN)
            .remote(f"unix://{unix_socket}")
            .file(TEST_NIX, "simple")
            .with_env(nix_env)
            .run()
        )
        assert rc == 0
        _valid_path = stdout.strip()

        # 2. Query validity through pynixd
        # We can use nix-store --query --path or similar, but simpler to just
        # try building it again which will trigger IsValidPath checks
        rc, _, _ = await (
            nix_command(LIX_BIN)
            .remote(f"unix://{unix_socket}")
            .file(TEST_NIX, "simple")
            .with_env(nix_env)
            .run()
        )
        assert rc == 0
