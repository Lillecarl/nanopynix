"""Functional tests for HTTP binary cache upload (PUT)."""

from __future__ import annotations

import os
from pathlib import Path

import aiohttp
import pytest
import structlog

from pynixd import Server
from pynixd.http_cache import format_narinfo
from pynixd.operations.query_all_valid_paths import QueryAllValidPathsRequest
from pynixd.operations.query_path_info import QueryPathInfoRequest
from pynixd.operations.nar_from_path import NarFromPathRequest
from pynixd.store import LocalSocketStore
from pynixd.store_path import StorePath
from tests.conftest import STORE_PREFIX, rmtree_robust

log = structlog.get_logger(__name__)


async def _pick_random_path(store: LocalSocketStore) -> StorePath:
    """Pick an arbitrary valid path from the store."""
    resp = await store.execute(QueryAllValidPathsRequest())
    all_paths = resp.paths
    assert all_paths, "Store has no paths?!"
    for p in sorted(all_paths):
        if p.endswith(".drv"):
            continue
        info_resp = await store.execute(QueryPathInfoRequest(path=p))
        if info_resp.valid and info_resp.info and info_resp.info.nar_size > 0:
            return p
    return next(iter(all_paths))


@pytest.mark.timeout(30)
async def test_http_upload(tmp_path: Path) -> None:
    """Test uploading a path to the HTTP cache via PUT using aiohttp directly."""
    # 1. Source store (root) has the path
    root_store = LocalSocketStore(id="root", store_path=Path("/"))
    path = await _pick_random_path(root_store)
    hash_part = path.hash_part()

    # Get its NAR and narinfo
    info_resp = await root_store.execute(QueryPathInfoRequest(path=path))
    assert info_resp.valid and info_resp.info
    info = info_resp.info

    nar_data = bytearray()

    async def collect_nar(chunk: bytes):
        nar_data.extend(chunk)

    await root_store.execute(
        NarFromPathRequest(
            path=path, nar_size=info.nar_size, async_callback=collect_nar
        )
    )

    narinfo = format_narinfo(
        path=info.path,
        nar_hash=info.nar_hash,
        nar_size=info.nar_size,
        references=info.references,
        deriver=info.deriver,
        sigs=info.sigs,
        ca=info.ca,
    )

    # 2. Target store (temp) is empty
    target_store_path = STORE_PREFIX / "http-upload-target-direct"
    rmtree_robust(target_store_path)
    os.makedirs(target_store_path, exist_ok=True)
    target_store = LocalSocketStore(id="target", store_path=target_store_path)

    # Enable uploads in pynixd
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()

    async with Server(
        local_store=target_store, http_port=0, http_upload_dir=upload_dir
    ) as server:
        base_url = f"http://127.0.0.1:{server.http_bound_port}"

        async with aiohttp.ClientSession() as session:
            # 3. PUT NAR
            # Nix uses nar/<narhash>.nar
            # We'll use the nar_hash from info (strip sha256: if present)
            nar_hash_part = info.nar_hash.split(":")[-1]
            log.info("uploading_nar", hash=nar_hash_part, size=len(nar_data))
            async with session.put(
                f"{base_url}/nar/{nar_hash_part}.nar", data=nar_data
            ) as resp:
                assert resp.status == 200
                assert await resp.text() == "ok\n"

            # 4. PUT .narinfo
            log.info("uploading_narinfo", hash=hash_part)
            async with session.put(
                f"{base_url}/{hash_part}.narinfo", data=narinfo
            ) as resp:
                assert resp.status == 200
                assert await resp.text() == "ok\n"

        # 5. Verify it now exists in the target store
        info_resp = await target_store.execute(QueryPathInfoRequest(path=path))
        assert info_resp.valid, (
            f"Path {path} should be valid in target store after upload"
        )
        assert info_resp.info is not None
        assert info_resp.info.path == path
        assert info_resp.info.nar_hash.split(":")[-1] == info.nar_hash.split(":")[-1]
        assert info_resp.info.nar_size == info.nar_size
