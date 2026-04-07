"""Functional tests for the HTTP binary cache server."""

from __future__ import annotations

import os
from pathlib import Path

import aiohttp
import pytest
import structlog

from pynixd import Server
from pynixd.operations.query_all_valid_paths import QueryAllValidPathsRequest
from pynixd.operations.query_path_info import QueryPathInfoRequest
from pynixd.store import LocalSocketStore
from pynixd.store_path import StorePath
from tests.conftest import STORE_PREFIX, run_captured

log = structlog.get_logger(__name__)


async def _pick_random_path(store: LocalSocketStore) -> StorePath:
    """Pick an arbitrary valid path from the store."""
    resp = await store.execute(QueryAllValidPathsRequest())
    all_paths = resp.paths
    assert all_paths, "Store has no paths?!"
    # Filter for something that likely has metadata
    for p in sorted(all_paths):
        if p.endswith(".drv"):
            continue
        # Also ensure it has some size
        info_resp = await store.execute(QueryPathInfoRequest(path=p))
        if info_resp.valid and info_resp.info and info_resp.info.nar_size > 0:
            return p
    return next(iter(all_paths))


@pytest.mark.timeout(30)
async def test_narinfo() -> None:
    """Test fetching .narinfo from the HTTP cache."""
    local_store = LocalSocketStore(id="local", store_path=Path("/"))

    async with Server(local_store=local_store, http_port=0) as server:
        path = await _pick_random_path(local_store)
        hash_part = path.hash_part()
        base_url = f"http://127.0.0.1:{server.http_bound_port}"
        log.info("test_narinfo", path=path, hash_part=hash_part, url=base_url)

        async with aiohttp.ClientSession() as session:
            url = f"{base_url}/{hash_part}.narinfo"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                assert resp.status == 200
                text = await resp.text()
                assert f"StorePath: {path}" in text
                assert "URL: nar/" in text


@pytest.mark.timeout(30)
async def test_nar_streaming() -> None:
    """Test streaming a NAR from the HTTP cache."""
    local_store = LocalSocketStore(id="local", store_path=Path("/"))

    async with Server(local_store=local_store, http_port=0) as server:
        path = await _pick_random_path(local_store)
        hash_part = path.hash_part()
        base_url = f"http://127.0.0.1:{server.http_bound_port}"

        # Get .narinfo to find the NAR URL
        async with aiohttp.ClientSession() as session:
            url = f"{base_url}/{hash_part}.narinfo"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                assert resp.status == 200
                narinfo = await resp.text()
                nar_url = ""
                for line in narinfo.splitlines():
                    if line.startswith("URL: "):
                        nar_url = line.split(": ", 1)[1]
                        break
                assert nar_url

            # Stream the NAR and verify it's not empty
            async with session.get(
                f"{base_url}/{nar_url}", timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                assert resp.status == 200
                total_bytes = 0
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    total_bytes += len(chunk)
                assert total_bytes > 0


@pytest.mark.timeout(30)
async def test_cache_as_substituter() -> None:
    """Test using pynixd HTTP cache as a substituter for another nix build."""
    local_store = LocalSocketStore(id="local", store_path=Path("/"))

    async with Server(local_store=local_store, http_port=0) as server:
        target_path = await _pick_random_path(local_store)
        base_url = f"http://127.0.0.1:{server.http_bound_port}"

        # Use a fresh temporary store for substitution
        subst_store_path = STORE_PREFIX / "http-subst-functional"
        if subst_store_path.exists():
            import shutil

            shutil.rmtree(subst_store_path)
        os.makedirs(subst_store_path, exist_ok=True)

        # Build something that requires our target path
        # For simplicity, we just try to 'nix-store -r' it into the new store
        from tests.conftest import NIX_BIN

        cmd = [
            str(NIX_BIN),
            "copy",
            "--to",
            f"file://{subst_store_path}",
            "--from",
            base_url,
            str(target_path),
            "--option",
            "require-sigs",
            "false",
        ]

        rc, stdout, stderr = await run_captured(cmd)
        assert rc == 0, f"Copy via cache failed:\n{stderr}"

        # Verify it exists in the new store
        cmd = [
            str(NIX_BIN),
            "store",
            "ls",
            "--store",
            f"file://{str(subst_store_path)}",
            str(target_path),
        ]
        rc, stdout, stderr = await run_captured(cmd)
        assert rc == 0, f"path check failed:\n{stderr}"


@pytest.mark.timeout(30)
async def test_cache_not_found() -> None:
    """Test 404 response for non-existent paths."""
    local_store = LocalSocketStore(id="local", store_path=Path("/"))
    async with Server(local_store=local_store, http_port=0) as server:
        base_url = f"http://127.0.0.1:{server.http_bound_port}"
        async with aiohttp.ClientSession() as session:
            hash_part = "00000000000000000000000000000000"
            async with session.get(
                f"{base_url}/{hash_part}.narinfo",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                assert resp.status == 404
