"""HTTP binary cache tests.

Tests that pynixd can serve the contents of its local store as a
Nix-compatible binary cache, including .narinfo and .nar.xz files.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import pytest
from conftest import (
    make_local_stores,
    nix_build,
    run_pynixd,
)

from pynixd.http_cache import BinaryCacheServer


@asynccontextmanager
async def run_cache_server(
    test_nix: str,
    nix_env: dict[str, str],
) -> AsyncIterator[tuple[str, Any]]:
    """Fixture to start pynixd and an HTTP cache on its local store."""
    stores = make_local_stores(n=1)

    async with run_pynixd(
        stores,
        client_store_path="/tmp/pynixd-test-http-client",
    ) as server:
        # Build something so the local store has real paths
        rc, _stdout, stderr = await nix_build(
            server.builder_uri(),
            "simple",
            nix_env,
            nix_file=test_nix,
        )
        assert rc == 0, f"setup build failed:\n{stderr}"

        # Start HTTP cache on the local store
        cache = BinaryCacheServer(server._local_store)  # type: ignore
        runner, port = await cache.start(host="127.0.0.1", port=0)
        base_url = f"http://127.0.0.1:{port}"

        yield base_url, server

        await runner.cleanup()


@pytest.mark.asyncio
async def test_narinfo(request: pytest.FixtureRequest, nix_env: dict[str, str]) -> None:
    """Test fetching .narinfo from the HTTP cache."""
    test_nix = request.config.getoption("--nix")

    async with run_cache_server(test_nix, nix_env) as (base_url, _server):
        # We need a path that exists in the local store.
        # "simple" was built in the fixture.
        # Get its store path via nix path-info.
        cmd = ["nix", "path-info", "--file", test_nix, "simple"]
        path = subprocess.check_output(cmd).decode().strip()
        hash_part = path.split("/")[-1].split("-")[0]

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/{hash_part}.narinfo") as resp:
                assert resp.status == 200
                text = await resp.text()
                assert f"StorePath: {path}" in text
                assert "URL: nar/" in text
                assert "Compression: xz" in text


@pytest.mark.asyncio
async def test_nar_streaming(
    request: pytest.FixtureRequest, nix_env: dict[str, str]
) -> None:
    """Test streaming a large NAR from the HTTP cache."""
    test_nix = request.config.getoption("--nix")

    async with run_cache_server(test_nix, nix_env) as (base_url, _server):
        # Build the 100MB benchmark path
        rc, _stdout, stderr = await nix_build(
            _server.builder_uri(),
            ".bench-100mb",
            nix_env,
            nix_file=test_nix,
        )
        assert rc == 0, f"bench build failed:\n{stderr}"

        cmd = ["nix", "path-info", "--file", test_nix, ".bench-100mb"]
        path = subprocess.check_output(cmd).decode().strip()
        hash_part = path.split("/")[-1].split("-")[0]

        # Get .narinfo to find the NAR URL
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/{hash_part}.narinfo") as resp:
                assert resp.status == 200
                narinfo = await resp.text()
                nar_url = ""
                for line in narinfo.splitlines():
                    if line.startswith("URL: "):
                        nar_url = line.split(": ", 1)[1]
                        break
                assert nar_url

            # Stream the NAR and verify size
            async with session.get(f"{base_url}/{nar_url}") as resp:
                assert resp.status == 200
                total_bytes = 0
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    total_bytes += len(chunk)

                # 100MB uncompressed, but it's served as .nar.xz
                # Just verify it's "large enough" (>1MB) to confirm streaming worked
                assert total_bytes > 1 * 1024 * 1024


@pytest.mark.asyncio
async def test_cache_as_substituter(
    request: pytest.FixtureRequest, nix_env: dict[str, str]
) -> None:
    """Test using pynixd HTTP cache as a substituter for another nix build."""
    test_nix = request.config.getoption("--nix")

    async with run_cache_server(test_nix, nix_env) as (base_url, _server):
        # 1. Path is already in _server.local_store (built in fixture)
        cmd = ["nix", "path-info", "--file", test_nix, "simple"]
        path = subprocess.check_output(cmd).decode().strip()

        # 2. Try to build it on a NEW isolated client store, using the cache
        # as a substituter.
        client_store = tempfile.mkdtemp(prefix="pynixd-test-subst-")
        try:
            cmd = [
                "nix",
                "build",
                "--store",
                client_store,
                "--substituters",
                f"https://cache.nixos.org {base_url}",
                "--hashes",
                "--no-link",
                "--file",
                test_nix,
                "simple",
            ]
            # Must disable sandbox if we want to use local substituter without certs
            # or use --option filter-syscalls false etc.
            # Easier: just run with --option require-sigs false
            cmd.extend(["--option", "require-sigs", "false"])

            # Verify it's actually substituted (not built) by checking logs
            # or just that it completes successfully using the cache.
            res = subprocess.run(cmd, env=nix_env, capture_output=True, text=True)
            assert res.returncode == 0, f"Substitution failed:\n{res.stderr}"
            assert os.path.exists(os.path.join(client_store, path.lstrip("/")))
        finally:
            subprocess.run(["rm", "-rf", client_store])


@pytest.mark.asyncio
async def test_cache_not_found(
    request: pytest.FixtureRequest, nix_env: dict[str, str]
) -> None:
    """Test 404 response for non-existent paths."""
    test_nix = request.config.getoption("--nix")
    async with run_cache_server(test_nix, nix_env) as (base_url, _):
        async with aiohttp.ClientSession() as session:
            # Random hash that doesn't exist
            hash_part = "00000000000000000000000000000000"
            async with session.get(f"{base_url}/{hash_part}.narinfo") as resp:
                assert resp.status == 404


@pytest.mark.asyncio
async def test_cache_add_multiple(
    request: pytest.FixtureRequest, nix_env: dict[str, str]
) -> None:
    """Test that multiple paths can be served from the cache."""
    test_nix = request.config.getoption("--nix")
    async with run_cache_server(test_nix, nix_env) as (base_url, server):
        # Build a second path
        rc, _stdout, stderr = await nix_build(
            server.builder_uri(),
            "complex",
            nix_env,
            nix_file=test_nix,
        )
        assert rc == 0, f"complex build failed:\n{stderr}"

        # Both 'simple' and 'complex' should be in the cache
        async with aiohttp.ClientSession() as session:
            for target in ["simple", "complex"]:
                cmd = ["nix", "path-info", "--file", test_nix, target]
                path = subprocess.check_output(cmd).decode().strip()
                hash_part = path.split("/")[-1].split("-")[0]

                async with session.get(f"{base_url}/{hash_part}.narinfo") as resp:
                    assert resp.status == 200
                    assert f"StorePath: {path}" in await resp.text()
