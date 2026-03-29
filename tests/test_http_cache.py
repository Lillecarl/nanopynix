"""Tests for the HTTP binary cache server."""

from __future__ import annotations

import asyncio

import pytest

from aiohttp import BasicAuth, ClientSession

from pynixd.store import LocalSocketStore
from pynixd.http_cache import BinaryCacheServer
from pynixd.local_store_db import LocalStoreDB

from conftest import (
    NIX_BIN,
    _run_subprocess_with_timeout,
    make_local_stores,
    nix_build,
    run_pynixd,
)


@pytest.fixture
async def cache_with_paths(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
):
    """Start pynixd, build something, then start the HTTP cache on the local store."""
    test_nix = request.config.getoption("--nix")
    stores = make_local_stores(n=1, prefix="http-cache-builder")

    async with run_pynixd(
        stores,
        client_store_path="/tmp/pynixd-test-http-client",
    ) as server:
        # Build something so the local store has real paths
        rc, _, stderr = await nix_build(
            server.builder_uri(), server.client_store_path, nix_env,
            "--file", test_nix, "simple",
        )
        assert rc == 0, f"setup build failed:\n{stderr}"

        # Start HTTP cache on the local store
        cache = BinaryCacheServer(server._local_store)
        runner, port = await cache.start(host="127.0.0.1", port=0)
        base_url = f"http://127.0.0.1:{port}"

        yield base_url, server

        await runner.cleanup()


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_nix_cache_info(cache_with_paths):
    """GET /nix-cache-info returns valid cache metadata."""
    base_url, _ = cache_with_paths

    async with ClientSession() as session:
        async with session.get(f"{base_url}/nix-cache-info") as resp:
            assert resp.status == 200
            text = await resp.text()
            assert "StoreDir: /nix/store" in text
            assert "WantMassQuery: 1" in text
            assert "Priority:" in text


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_narinfo(cache_with_paths):
    """GET /{hash}.narinfo returns valid narinfo for known paths."""
    base_url, server = cache_with_paths
    db = server._local_store.db

    # Find a path in the local store
    result = db.query_all_valid_paths()
    assert result is not None and result.paths, "no paths in local store"
    path = next(iter(result.paths))
    hash_part = path.split("/")[3].split("-")[0]  # /nix/store/<hash>-name

    async with ClientSession() as session:
        async with session.get(f"{base_url}/{hash_part}.narinfo") as resp:
            assert resp.status == 200
            text = await resp.text()
            assert text.startswith("StorePath: /nix/store/")
            assert "NarHash: sha256:" in text
            assert "NarSize:" in text
            assert f"URL: nar/{hash_part}.nar" in text


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_narinfo_404(cache_with_paths):
    """GET /{hash}.narinfo returns 404 for unknown paths."""
    base_url, _ = cache_with_paths

    async with ClientSession() as session:
        async with session.get(f"{base_url}/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.narinfo") as resp:
            assert resp.status == 404


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_nar_download(cache_with_paths):
    """GET /nar/{hash}.nar returns correct NAR data."""
    base_url, server = cache_with_paths
    db = server._local_store.db

    # Find a small path
    result = db.query_all_valid_paths()
    assert result is not None and result.paths

    # Pick a path and get its expected size
    path = next(iter(result.paths))
    info = db.query_path_info(path)
    assert info is not None and info.valid
    expected_size = info.info.nar_size
    hash_part = path.split("/")[3].split("-")[0]

    async with ClientSession() as session:
        async with session.get(f"{base_url}/nar/{hash_part}.nar") as resp:
            assert resp.status == 200
            data = await resp.read()
            assert len(data) == expected_size
            # NAR magic: "\x0d\x00\x00\x00\x00\x00\x00\x00nix-archive-1"
            assert b"nix-archive-1" in data[:32]


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_nar_404(cache_with_paths):
    """GET /nar/{hash}.nar returns 404 for unknown paths."""
    base_url, _ = cache_with_paths

    async with ClientSession() as session:
        async with session.get(f"{base_url}/nar/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.nar") as resp:
            assert resp.status == 404


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_nix_substituter(cache_with_paths, nix_env: dict[str, str]):
    """A real nix client can fetch paths from our HTTP cache."""
    base_url, server = cache_with_paths
    db = server._local_store.db

    # Find the build output path
    result = db.query_all_valid_paths()
    assert result is not None and result.paths
    # Pick a path that looks like a build output (not a .drv)
    path = next(p for p in result.paths if not p.endswith(".drv"))

    # Copy from our HTTP cache into a fresh store
    dst_store = "/tmp/pynixd-test-http-nix-client"
    rc, stdout, stderr = await _run_subprocess_with_timeout(
        [
            NIX_BIN, "copy", "--from", base_url,
            "--to", dst_store,
            "--no-check-sigs",
            path,
        ],
        env=nix_env,
        timeout=30,
    )
    assert rc == 0, f"nix copy failed:\n{stderr}"

    # Verify the path exists in the destination store
    rc2, stdout2, stderr2 = await _run_subprocess_with_timeout(
        [
            NIX_BIN, "path-info", "--store", dst_store, path,
        ],
        env=nix_env,
        timeout=10,
    )
    assert rc2 == 0, f"path not found in destination store:\n{stderr2}"
    assert path in stdout2


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_basic_auth():
    """Server rejects requests without valid credentials."""
    local = LocalSocketStore(
        store_path="/tmp/pynixd-test-http-auth",
        id="local-auth",
        max_builds=0,
        nix_bin=NIX_BIN,
    )
    await local.probe_version()
    local.db = LocalStoreDB(local.store_path)

    cache = BinaryCacheServer(local, username="testuser", password="testpass")
    runner, port = await cache.start(host="127.0.0.1", port=0)
    base_url = f"http://127.0.0.1:{port}"

    try:
        async with ClientSession() as session:
            # No auth → 401
            async with session.get(f"{base_url}/nix-cache-info") as resp:
                assert resp.status == 401

            # Wrong auth → 403
            async with session.get(
                f"{base_url}/nix-cache-info",
                auth=BasicAuth("wrong", "wrong"),
            ) as resp:
                assert resp.status == 403

            # Correct auth → 200
            async with session.get(
                f"{base_url}/nix-cache-info",
                auth=BasicAuth("testuser", "testpass"),
            ) as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "StoreDir: /nix/store" in text
    finally:
        await runner.cleanup()
        if local.db is not None:
            local.db.close()
        await local.close()
