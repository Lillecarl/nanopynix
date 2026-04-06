"""Simple end-to-end build tests."""

from __future__ import annotations

from pathlib import Path

from pynixd import Server
from pynixd.instance import NixImplementation
from pynixd.store import LocalSocketStore
from tests.conftest import LIX_BIN, NIX_BIN, STORE_PREFIX, run_captured


async def test_builders() -> None:
    """Build test.nix .simple via --builders."""
    test_nix = Path("test.nix")
    store_path = STORE_PREFIX / "builders"
    local_store = LocalSocketStore(id="local", store_path=store_path, nix_bin=NIX_BIN)
    builder_store = LocalSocketStore(
        id="builder", store_path=store_path, nix_bin=NIX_BIN
    )

    async with Server(
        local_store=local_store, stores={"builder": builder_store}, ssh_port=0
    ) as server:
        uri = server.builder_uri(implementation=NixImplementation.NIX, max_jobs=1)
        cmd = [
            str(NIX_BIN),
            "build",
            "--builders",
            uri,
            "--file",
            str(test_nix),
            "simple",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr = await run_captured(cmd)
        assert rc == 0, f"build failed:\n{stderr}"


async def test_store() -> None:
    """Build test.nix .simple via --store."""
    test_nix = Path("test.nix")
    store_path = STORE_PREFIX / "store"
    local_store = LocalSocketStore(id="local", store_path=store_path, nix_bin=NIX_BIN)
    builder_store = LocalSocketStore(
        id="builder", store_path=store_path, nix_bin=NIX_BIN
    )

    async with Server(
        local_store=local_store, stores={"builder": builder_store}, ssh_port=0
    ) as server:
        uri = server.uri(implementation=NixImplementation.NIX)
        cmd = [
            str(NIX_BIN),
            "build",
            "--store",
            uri,
            "--file",
            str(test_nix),
            "simple",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr = await run_captured(cmd)
        assert rc == 0, f"build failed:\n{stderr}"
