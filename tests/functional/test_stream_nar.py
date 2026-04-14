"""Tests for NAR streaming between stores."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from pynixd.operations.is_valid_path import IsValidPathRequest
from pynixd.store import LocalSocketStore, Store
from pynixd.store_path import StorePath
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    get_test_store_kwargs,
    run_subproc,
    rmtree_robust,
)

log = structlog.get_logger(__name__)


async def get_hello_path() -> StorePath:
    """Build nixpkgs#hello and return its store path."""
    rc, stdout, stderr, _ = await run_subproc(
        [str(NIX_BIN), "build", "nixpkgs#hello", "--no-link", "--print-out-paths"]
    )
    return StorePath(stdout.strip())


@pytest.mark.asyncio
async def test_stream_nar() -> None:
    """
    Test streaming a NAR from the system store to a temporary store.
    This doesn't start pynixd, it just uses two LocalSocketStore instances.
    """
    async with asyncio.timeout(50):
        # 1. Build hello in the system store
        store_path = await get_hello_path()
        log.info("streaming_path", path=store_path)

        # Source store is the system store (/)
        src_store = LocalSocketStore(
            id="system",
            store_path=Path("/"),
            **get_test_store_kwargs(),
        )

        # Destination store is a temporary store
        dst_path = STORE_PREFIX / "test-stream-nar"
        rmtree_robust(dst_path)
        dst_store = LocalSocketStore(
            id="test-stream-nar",
            store_path=dst_path,
            **get_test_store_kwargs(),
        )

        try:
            # 2. Stream the path from src to dst
            # Check if path exists in src
            is_valid_src = await src_store.execute(IsValidPathRequest(path=store_path))
            assert is_valid_src.valid, f"Path {store_path} not valid in system store"

            # Use stream_paths_store_to_store which handles the NAR piping
            await Store.stream_paths_store_to_store(src_store, dst_store, [store_path])

            # Verify it now exists in dst
            is_valid_dst_after = await dst_store.execute(
                IsValidPathRequest(path=store_path)
            )
            assert is_valid_dst_after.valid
        finally:
            await src_store.close()
            await dst_store.close()
