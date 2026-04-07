"""Tests for NAR streaming between stores."""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from pynixd.store import LocalSocketStore
from pynixd.store_path import StorePath
from tests.conftest import NIX_BIN, STORE_PREFIX

log = structlog.get_logger(__name__)


async def _pick_a_path(store: LocalSocketStore) -> StorePath:
    """Pick an arbitrary valid path from the store."""
    all_paths = await store.query_all_valid_paths()
    assert all_paths, "Store has no paths?!"
    for p in sorted(all_paths):
        if p.endswith(".drv"):
            continue
        info = await store.query_path_info(p)
        # Pick something small-ish
        if info and 0 < info.nar_size < 1_000_000:
            return p
    pytest.fail("Could not find a suitable path in store")


@pytest.mark.asyncio
async def test_stream_nar() -> None:
    """
    Test streaming a NAR from the system store to a temporary store.
    This doesn't start pynixd, it just uses two LocalSocketStore instances.
    """
    # Source store is the system store (/)
    src_store = LocalSocketStore(id="system", store_path=Path("/"), nix_bin=NIX_BIN)

    # Destination store is a temporary store
    dst_path = STORE_PREFIX / "test-stream-nar"
    dst_store = LocalSocketStore(
        id="test-stream-nar", store_path=dst_path, nix_bin=NIX_BIN
    )

    try:
        # 1. Pick a path from the system store
        store_path = await _pick_a_path(src_store)
        log.info("streaming_path", path=store_path)

        # 2. Stream the path from src to dst
        # Check if path exists in src
        assert await src_store.is_valid_path(store_path), (
            f"Path {store_path} not valid in system store"
        )

        # Ensure it doesn't exist in dst (or at least we hope so for a fresh store)
        # If it happens to be there, we'll still test the streaming logic
        if await dst_store.is_valid_path(store_path):
            log.warning("path_already_in_dst", path=store_path)

        # Use stream_paths_to which handles the NAR piping
        await src_store.stream_paths_to(dst_store, [store_path])

        # Verify it now exists in dst
        assert await dst_store.is_valid_path(store_path)
    finally:
        await src_store.close()
        await dst_store.close()
