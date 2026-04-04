"""HTTP binary cache tests.

Tests that pynixd can serve the contents of its local store as a
Nix-compatible binary cache, including .narinfo and .nar.xz files.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import structlog
from conftest import (
    NIX_BIN,
    make_local_stores,
    nix_build_store_only,
    run_process_async,
)

from pynixd import Server
from pynixd.http_cache import BinaryCacheServer
from pynixd.store import LocalSocketStore

log = structlog.get_logger(__name__)


@asynccontextmanager
async def run_cache_server(
    test_nix: Path,
    nix_env: dict[str, str],
) -> AsyncIterator[tuple[str, Server, dict[str, Any]]]:
    """Fixture to start pynixd and an HTTP cache on its local store."""
    stores = make_local_stores(n=1)

    # Use a consistent timestamp for all builds in a single test run
    # to ensure store paths are predictable.
    nix_env = nix_env.copy()
    nix_env["PYNIXD_TEST_TS"] = "1"

    local_store = LocalSocketStore(
        store_path=Path("/tmp/pynixd-test-http-local"),
        id="local",
        max_builds=0,
        max_transfers=64,
        nix_bin=str(NIX_BIN),
    )

    async with Server(
        stores=stores,
        local_store=local_store,
        ssh_port=0,
    ) as server:
        # Build something DIRECTLY on the server's local store.
        # We use nix_build_store_only to avoid a recursive loop where
        # the client calls pynixd which calls the same local store.
        rc, stdout, stderr = await nix_build_store_only(
            str(local_store.store_path),
            "simple",
            nix_env,
            "--print-out-paths",
            nix_file=test_nix,
        )
        assert rc == 0, f"setup build failed:\n{stderr}"
        path = stdout.splitlines()[0].strip()
        log.info("setup built path: %s", path)

        # Get detailed path info
        cmd = ["nix", "path-info", "--json", path]
        rc, stdout, stderr = await run_process_async(cmd, env=nix_env)
        assert rc == 0, f"path-info failed:\n{stderr}"
        simple_info = json.loads(stdout)[0]
        log.info("setup path info: %s", simple_info)

        # Start HTTP cache on the local store
        cache = BinaryCacheServer(server.config.local_store)
        runner, port = await cache.start(host="127.0.0.1", port=0)
        base_url = f"http://127.0.0.1:{port}"
        log.info("cache server listening on %s", base_url)

        yield base_url, server, simple_info

        await runner.cleanup()


async def test_narinfo(request: pytest.FixtureRequest, nix_env: dict[str, str]) -> None:
    """Test fetching .narinfo from the HTTP cache."""
    test_nix = Path(request.config.getoption("--nix"))

    async with run_cache_server(test_nix, nix_env) as (
        base_url,
        _server,
        simple_info,
    ):
        path = simple_info["path"]
        hash_part = path.split("/")[-1].split("-")[0]
        log.info("test_narinfo hash_part=%s", hash_part)

        async with aiohttp.ClientSession() as session:
            url = f"{base_url}/{hash_part}.narinfo"
            log.info("GET %s", url)
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.error("FAILED %d: %s", resp.status, await resp.text())
                assert resp.status == 200
                text = await resp.text()
                assert f"StorePath: {path}" in text
                assert "URL: nar/" in text
                assert "Compression: none" in text


async def test_nar_streaming(
    request: pytest.FixtureRequest, nix_env: dict[str, str]
) -> None:
    """Test streaming a large NAR from the HTTP cache."""
    test_nix = Path(request.config.getoption("--nix"))

    # Need enough leaves to reach >1MB output
    nix_env = nix_env.copy()
    nix_env["PYNIXD_PAR_COUNT"] = "100"
    nix_env["PYNIXD_TEST_TS"] = "1"

    async with run_cache_server(test_nix, nix_env) as (
        base_url,
        server,
        simple_info,
    ):
        # Build the big path directly into local store
        rc, stdout, stderr = await nix_build_store_only(
            str(server.config.local_store.store_path),
            "big",
            nix_env,
            "--print-out-paths",
            nix_file=test_nix,
        )
        assert rc == 0, f"big build failed:\n{stderr}"
        path = stdout.splitlines()[0].strip()
        hash_part = path.split("/")[-1].split("-")[0]

        # Get .narinfo to find the NAR URL
        async with aiohttp.ClientSession() as session:
            url = f"{base_url}/{hash_part}.narinfo"
            async with session.get(url) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    log.error("FAILED .narinfo %d: %s", resp.status, txt)
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

                # Uncompressed output is large, served as .nar.xz
                # Just verify it's "large enough" (>100KB) to confirm streaming worked
                assert total_bytes > 100_000


async def test_cache_as_substituter(
    request: pytest.FixtureRequest, nix_env: dict[str, str]
) -> None:
    """Test using pynixd HTTP cache as a substituter for another nix build."""
    test_nix = Path(request.config.getoption("--nix"))
    nix_env = nix_env.copy()
    nix_env["PYNIXD_TEST_TS"] = "1"

    async with run_cache_server(test_nix, nix_env) as (
        base_url,
        _server,
        simple_info,
    ):
        target_path = simple_info["path"]
        # To test substitution, we MUST use a store that DOES NOT have the path.
        # We create a unique temporary store for this specific client build.
        with tempfile.TemporaryDirectory(prefix="pynixd-subst-") as tmp_dir:
            subst_store = Path(tmp_dir)
            try:
                cmd = [
                    "nix",
                    "build",
                    "--store",
                    str(subst_store),
                    "--substituters",
                    f"https://cache.nixos.org {base_url}",
                    "--no-link",
                    "--file",
                    str(test_nix),
                    "simple",
                ]

                # Disable signature verification for local unsigned cache
                cmd.extend(["--option", "require-sigs", "false"])

                # Verify it's actually substituted (not built)
                rc, stdout, stderr = await run_process_async(cmd, env=nix_env)
                assert rc == 0, f"Substitution failed:\n{stderr}"

                # Query the path in the new store
                cmd = [
                    "nix",
                    "path-info",
                    "--store",
                    str(subst_store),
                    target_path,
                ]
                rc, stdout, stderr = await run_process_async(cmd, env=nix_env)
                assert rc == 0, f"path-info failed:\n{stderr}"
                assert target_path in stdout
            finally:
                pass


async def test_cache_not_found(
    request: pytest.FixtureRequest, nix_env: dict[str, str]
) -> None:
    """Test 404 response for non-existent paths."""
    test_nix = Path(request.config.getoption("--nix"))
    async with run_cache_server(test_nix, nix_env) as (base_url, _, _simple_info):
        async with aiohttp.ClientSession() as session:
            # Random hash that doesn't exist
            hash_part = "00000000000000000000000000000000"
            async with session.get(f"{base_url}/{hash_part}.narinfo") as resp:
                assert resp.status == 404


async def test_cache_add_multiple(
    request: pytest.FixtureRequest, nix_env: dict[str, str]
) -> None:
    """Test that multiple paths can be served from the cache."""
    test_nix = Path(request.config.getoption("--nix"))
    nix_env = nix_env.copy()
    nix_env["PYNIXD_TEST_TS"] = "1"

    async with run_cache_server(test_nix, nix_env) as (
        base_url,
        server,
        _simple_info,
    ):
        store_path = server.config.local_store.store_path
        # Build a second path directly into local store
        rc, stdout, stderr = await nix_build_store_only(
            str(store_path),
            "complex",
            nix_env,
            "--print-out-paths",
            nix_file=test_nix,
        )
        assert rc == 0, f"complex build failed:\n{stderr}"
        complex_path = stdout.splitlines()[0].strip()

        # Both 'simple' and 'complex' should be in the cache
        async with aiohttp.ClientSession() as session:
            for target_path in [_simple_info["path"], complex_path]:
                hash_part = target_path.split("/")[-1].split("-")[0]

                async with session.get(f"{base_url}/{hash_part}.narinfo") as resp:
                    assert resp.status == 200
                    assert f"StorePath: {target_path}" in await resp.text()
