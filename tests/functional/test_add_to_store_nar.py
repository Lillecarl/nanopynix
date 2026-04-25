"""Functional tests for AddToStoreNar operation (single path, no refs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pynixd import Server
from pynixd.operations.query_path_info import QueryPathInfoRequest
from pynixd.store_path import StorePath
from tests.conftest import (
    NIX_BIN,
    run_subproc,
)


@pytest.mark.timeout(50)
async def test_add_to_store_nar(pynixd_server: Server, tmp_path: Path):
    """Add a path to the store using nix store add-path to trigger AddToStoreNar.

    Store operations triggered:
    - IsValidPath: Checks if path exists
    - AddToStoreNar: Adds the NAR to the store
    """
    target_store = pynixd_server.local_store

    test_file = tmp_path / "test-file"
    test_file.write_text("some random content")

    rc, stdout, stderr, _ = await run_subproc(
        [
            str(NIX_BIN),
            "store",
            "add-path",
            "--store",
            pynixd_server.uri(),
            str(test_file),
        ],
    )
    assert rc == 0, f"nix store add-path failed:\n{stderr}"
    path = StorePath(stdout.strip())

    info_resp = await target_store.execute(QueryPathInfoRequest(path=path))
    assert info_resp.valid, f"Path {path} should be valid in target store"
    assert info_resp.info is not None, "Info should not be none"
    assert not info_resp.info.references, "Path should have no references"
