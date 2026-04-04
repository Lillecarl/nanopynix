"""pynixd end-to-end benchmarks.

Measures pynixd performance through the full stack:
- SSH daemon protocol (asyncssh client → pynixd → local store)
- HTTP binary cache (aiohttp client → pynixd → local store)

Each benchmark runs with and without pool warming to measure cold-start
vs steady-state performance.

Concurrency levels test how well pynixd handles parallel clients/channels.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
from pathlib import Path

import aiohttp
import pytest
import structlog
from conftest import (
    NIX_BIN,
    _record,
)
from environs import Env

from pynixd import Server
from pynixd.http_cache import BinaryCacheServer
from pynixd.operations.base import PathInfo
from pynixd.store import LocalSocketStore, SSHSubprocessStore, Store

log = structlog.get_logger(__name__)

env = Env()

_SSH_USER = env.str("USER", "root")

_CONCURRENCY_LEVELS = [1, 10, 50]


# ── Helpers ───────────────────────────────────────────────────────


def _create_big_path(size_mb: int) -> str:
    """Create a large file in the nix store via nix store add."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bench") as f:
        chunk = os.urandom(1024 * 1024)
        for _ in range(size_mb):
            f.write(chunk)
        f.flush()
        tmp_path = Path(f.name)

    try:
        result = subprocess.run(
            [
                str(NIX_BIN),
                "store",
                "add",
                "--name",
                f"bench-{size_mb}mb",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"nix store add failed: {result.stderr}"
        return result.stdout.strip()
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


async def _pick_small_paths(
    src: Store,
    count: int,
) -> list[tuple[str, PathInfo]]:
    """Pick self-contained paths with nar_size < 50KB from the system store."""
    all_paths = await src.query_all_valid_paths()
    picked: list[tuple[str, PathInfo]] = []
    for p in sorted(all_paths):
        if len(picked) >= count:
            break
        if p.endswith(".drv"):
            continue
        info = await src.query_path_info(p)
        if info and 0 < info.nar_size < 50_000:
            if info.references - {p}:
                continue
            picked.append((p, info))
    return picked


def _hash_part(path: str) -> str:
    """'/nix/store/abc-foo' → 'abc'"""
    return path.removeprefix("/nix/store/").split("-", 1)[0]


# ── SSH daemon protocol benchmarks ──────────────────────────────


@pytest.mark.timeout(300)
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.bench
async def test_ssh_serve_small_nars(
    request: pytest.FixtureRequest,
    concurrency: int,
    warm: bool,
) -> None:
    """Benchmark: fetch many small NARs from pynixd over SSH."""
    local_store = LocalSocketStore(id="bench-local", max_transfers=20)

    picked = await _pick_small_paths(local_store, 500)
    assert len(picked) >= 100, f"Need 100+ small paths, found {len(picked)}"

    async with Server(
        stores={},
        local_store=local_store,
        ssh_port=0,
    ) as server:
        client = SSHSubprocessStore(
            host=server.host,
            port=server.port,
            id="bench-client",
            username=server.username,
            max_transfers=concurrency + 2,
            monitor=False,
        )
        try:
            if warm:
                await client.warm_pool(concurrency)

            sem = asyncio.Semaphore(concurrency)
            total_bytes = 0
            lock = asyncio.Lock()

            async def _fetch(path: str, nar_size: int) -> None:
                nonlocal total_bytes
                async with sem:
                    data = await client.buffer_nar_from_path(path, nar_size=nar_size)
                async with lock:
                    total_bytes += len(data)

            warmth = "warm" if warm else "cold"
            start = time.monotonic()
            await asyncio.gather(*[_fetch(p, i.nar_size) for p, i in picked])
            elapsed = time.monotonic() - start
        finally:
            await client.close()

    mb_per_s = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    ops_per_s = len(picked) / elapsed if elapsed > 0 else 0
    _record(
        request,
        f"ssh serve small c={concurrency} {warmth}",
        elapsed=f"{elapsed:.1f}s",
        throughput=f"{mb_per_s:.1f} MB/s",
        ops=f"{ops_per_s:.0f} ops/s",
    )


@pytest.mark.timeout(300)
@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.bench
async def test_ssh_serve_big_nar(
    request: pytest.FixtureRequest,
    warm: bool,
) -> None:
    """Benchmark: fetch a 100MB NAR from pynixd over SSH."""
    store_path = _create_big_path(100)
    local_store = LocalSocketStore(id="bench-local", max_transfers=20)
    info = await local_store.query_path_info(store_path)
    assert info is not None

    async with Server(
        stores={},
        local_store=local_store,
        ssh_port=0,
    ) as server:
        client = SSHSubprocessStore(
            host=server.host,
            port=server.port,
            id="bench-client",
            username=server.username,
            max_transfers=4,
            monitor=False,
        )
        try:
            if warm:
                await client.warm_pool(1)

            start = time.monotonic()
            data = await client.buffer_nar_from_path(store_path, nar_size=info.nar_size)
            elapsed = time.monotonic() - start
        finally:
            await client.close()

    warmth = "warm" if warm else "cold"
    mb_per_s = (len(data) / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    _record(
        request,
        f"ssh serve big {warmth}",
        elapsed=f"{elapsed:.1f}s",
        throughput=f"{mb_per_s:.1f} MB/s",
    )


@pytest.mark.timeout(300)
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.bench
async def test_ssh_query_path_info(
    request: pytest.FixtureRequest,
    concurrency: int,
    warm: bool,
) -> None:
    """Benchmark: QueryPathInfo throughput through pynixd over SSH."""
    local_store = LocalSocketStore(id="bench-local", max_transfers=20)

    picked = await _pick_small_paths(local_store, 500)
    assert len(picked) >= 100, f"Need 100+ small paths, found {len(picked)}"

    async with Server(
        stores={},
        local_store=local_store,
        ssh_port=0,
    ) as server:
        client = SSHSubprocessStore(
            host=server.host,
            port=server.port,
            id="bench-client",
            username=server.username,
            max_transfers=concurrency + 2,
            monitor=False,
        )
        try:
            if warm:
                await client.warm_pool(concurrency)

            sem = asyncio.Semaphore(concurrency)

            async def _query(path: str) -> None:
                async with sem:
                    await client.query_path_info(path)

            warmth = "warm" if warm else "cold"
            start = time.monotonic()
            await asyncio.gather(*[_query(p) for p, _ in picked])
            elapsed = time.monotonic() - start
        finally:
            await client.close()

    ops_per_s = len(picked) / elapsed if elapsed > 0 else 0
    _record(
        request,
        f"ssh query c={concurrency} {warmth}",
        elapsed=f"{elapsed:.1f}s",
        ops=f"{ops_per_s:.0f} ops/s",
    )


# ── HTTP binary cache benchmarks ─────────────────────────────────


@pytest.mark.timeout(300)
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
@pytest.mark.bench
async def test_http_serve_small_nars(
    request: pytest.FixtureRequest,
    concurrency: int,
) -> None:
    """Benchmark: fetch many small NARs from pynixd HTTP cache."""
    local_store = LocalSocketStore(id="bench-local", max_transfers=20)

    picked = await _pick_small_paths(local_store, 500)
    assert len(picked) >= 100, f"Need 100+ small paths, found {len(picked)}"

    cache = BinaryCacheServer(local_store)
    runner, http_port = await cache.start(host="127.0.0.1", port=0)
    base_url = f"http://127.0.0.1:{http_port}"

    try:
        sem = asyncio.Semaphore(concurrency)
        total_bytes = 0
        lock = asyncio.Lock()

        async with aiohttp.ClientSession() as session:

            async def _fetch(path: str) -> None:
                nonlocal total_bytes
                hash_part = _hash_part(path)
                async with sem:
                    async with session.get(f"{base_url}/nar/{hash_part}.nar") as resp:
                        assert resp.status == 200, f"HTTP {resp.status} for {path}"
                        data = await resp.read()
                async with lock:
                    total_bytes += len(data)

            start = time.monotonic()
            await asyncio.gather(*[_fetch(p) for p, _ in picked])
            elapsed = time.monotonic() - start
    finally:
        await runner.cleanup()
        await local_store.close()

    mb_per_s = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    ops_per_s = len(picked) / elapsed if elapsed > 0 else 0
    _record(
        request,
        f"http serve small c={concurrency}",
        elapsed=f"{elapsed:.1f}s",
        throughput=f"{mb_per_s:.1f} MB/s",
        ops=f"{ops_per_s:.0f} ops/s",
    )


@pytest.mark.timeout(300)
@pytest.mark.bench
async def test_http_serve_big_nar(
    request: pytest.FixtureRequest,
) -> None:
    """Benchmark: fetch a 100MB NAR from pynixd HTTP cache."""
    store_path = _create_big_path(100)
    local_store = LocalSocketStore(id="bench-local", max_transfers=20)
    info = await local_store.query_path_info(store_path)
    assert info is not None

    cache = BinaryCacheServer(local_store)
    runner, http_port = await cache.start(host="127.0.0.1", port=0)
    base_url = f"http://127.0.0.1:{http_port}"
    hash_part = _hash_part(store_path)

    try:
        async with aiohttp.ClientSession() as session:
            start = time.monotonic()
            async with session.get(f"{base_url}/nar/{hash_part}.nar") as resp:
                assert resp.status == 200
                data = await resp.read()
            elapsed = time.monotonic() - start
    finally:
        await runner.cleanup()
        await local_store.close()

    mb_per_s = (len(data) / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    _record(
        request,
        "http serve big",
        elapsed=f"{elapsed:.1f}s",
        throughput=f"{mb_per_s:.1f} MB/s",
    )


@pytest.mark.timeout(300)
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
@pytest.mark.bench
async def test_http_narinfo(
    request: pytest.FixtureRequest,
    concurrency: int,
) -> None:
    """Benchmark: narinfo lookup throughput from pynixd HTTP cache."""
    local_store = LocalSocketStore(id="bench-local", max_transfers=20)

    picked = await _pick_small_paths(local_store, 500)
    assert len(picked) >= 100, f"Need 100+ small paths, found {len(picked)}"

    cache = BinaryCacheServer(local_store)
    runner, http_port = await cache.start(host="127.0.0.1", port=0)
    base_url = f"http://127.0.0.1:{http_port}"

    try:
        sem = asyncio.Semaphore(concurrency)

        async with aiohttp.ClientSession() as session:

            async def _fetch_narinfo(path: str) -> None:
                hash_part = _hash_part(path)
                async with sem:
                    async with session.get(f"{base_url}/{hash_part}.narinfo") as resp:
                        assert resp.status == 200, f"HTTP {resp.status} for {path}"
                        await resp.read()

            start = time.monotonic()
            await asyncio.gather(*[_fetch_narinfo(p) for p, _ in picked])
            elapsed = time.monotonic() - start
    finally:
        await runner.cleanup()
        await local_store.close()

    ops_per_s = len(picked) / elapsed if elapsed > 0 else 0
    _record(
        request,
        f"http narinfo c={concurrency}",
        elapsed=f"{elapsed:.1f}s",
        ops=f"{ops_per_s:.0f} ops/s",
    )
