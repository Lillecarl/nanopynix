"""Functional tests for AddToStoreNar operation (single path, no refs)."""

from __future__ import annotations
from pynixd.store_path import StorePath

import asyncio
from pathlib import Path

from pynixd import Server
from pynixd.store import LocalSocketStore
from pynixd.operations.query_path_info import QueryPathInfoRequest
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    get_test_store_kwargs,
    run_subproc,
    rmtree_robust,
)

LOCAL_STORE = STORE_PREFIX / "add-to-store-nar-test"


async def test_add_to_store_nar(tmp_path: Path):
    """Add a path to the store using nix store add-path to trigger AddToStoreNar."""
    async with asyncio.timeout(50):
        rmtree_robust(LOCAL_STORE)
        target_store = LocalSocketStore(
            id="local",
            store_path=LOCAL_STORE,
            **get_test_store_kwargs(),
        )

        async with Server(
            local_store=target_store,
            ssh_port=0,
        ) as server:
            # Create a test file
            test_file = tmp_path / "test-file"
            test_file.write_text("some random content")

            # Add to pynixd using nix store add-path
            # This is expected to use AddToStoreNar (39) or similar.
            rc, stdout, stderr, _ = await run_subproc(
                [
                    str(NIX_BIN),
                    "store",
                    "add-path",
                    "--store",
                    server.uri(),
                    str(test_file),
                ]
            )
            assert rc == 0, f"nix store add-path failed:\n{stderr}"
            path = StorePath(stdout.strip())

            # Verify it exists in target store
            info_resp = await target_store.execute(QueryPathInfoRequest(path=path))
            assert info_resp.valid, f"Path {path} should be valid in target store"
            assert info_resp.info is not None, "Info should not be none"
            assert not info_resp.info.references, "Path should have no references"
