"""pynixd end-to-end benchmarks.

Measures pynixd performance through the full stack:
- SSH daemon protocol (asyncssh client → pynixd → local store)
- HTTP binary cache (aiohttp client → pynixd → local store)
- Build dispatch (nix client → pynixd → backend stores)

Each benchmark runs with and without pool warming to measure cold-start
vs steady-state performance.

Concurrency levels test how well pynixd handles parallel clients/channels.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass

import aiohttp
import pyinstrument
import pytest
from conftest import (
    NIX_BIN,
    _run_subprocess_with_timeout,
    make_local_stores,
    run_pynixd,
)
from environs import Env

from pynixd.http_cache import BinaryCacheServer
from pynixd.operations.base import PathInfo
from pynixd.store import LocalSocketStore, SSHSubprocessStore, Store

log = logging.getLogger(__name__)

env = Env()

pytestmark = pytest.mark.benchmark

_SSH_USER = env.str("USER", "root")

_CONCURRENCY_LEVELS = [1, 10, 50]

_PROFILE = env.str("PYNIXD_PROFILE", "")


def _profile_context(test_name: str):
    """Return a pyinstrument profiler context if PYNIXD_PROFILE is set.

    Usage:
        with _profile_context("test_name") as p:
            ... # hot path
        # prints profile tree on exit
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        if not _PROFILE:
            yield None
            return
        profiler = pyinstrument.Profiler(async_mode="enabled")
        profiler.start()
        try:
            yield profiler
        finally:
            profiler.stop()
            print(f"\n{'=' * 60}")
            print(f"PROFILE: {test_name}")
            print(f"{'=' * 60}")
            print(profiler.output_text(unicode=True, color=True, show_all=True))

    return _ctx()


# ── Result collection ────────────────────────────────────────────


@dataclass
class PynixdBenchResult:
    label: str
    elapsed: float
    count: int
    total_bytes: int = 0

    @property
    def ops_per_s(self) -> float:
        return self.count / self.elapsed if self.elapsed > 0 else 0

    @property
    def mb_per_s(self) -> float:
        if self.total_bytes == 0:
            return 0
        return (
            (self.total_bytes / (1024 * 1024)) / self.elapsed if self.elapsed > 0 else 0
        )


def _record(
    request: pytest.FixtureRequest,
    label: str,
    elapsed: float,
    count: int,
    total_bytes: int = 0,
) -> None:
    r = PynixdBenchResult(label, elapsed, count, total_bytes)
    results = request.config.stash.setdefault(_pynixd_bench_key, [])
    results.append(r)


_pynixd_bench_key = pytest.StashKey[list[PynixdBenchResult]]()


# ── Helpers ───────────────────────────────────────────────────────


def _create_big_path(size_mb: int) -> str:
    """Create a large file in the nix store via nix store add."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bench") as f:
        chunk = os.urandom(1024 * 1024)
        for _ in range(size_mb):
            f.write(chunk)
        f.flush()
        tmp_path = f.name

    try:
        result = subprocess.run(
            [NIX_BIN, "store", "add", "--name", f"bench-{size_mb}mb", tmp_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"nix store add failed: {result.stderr}"
        return result.stdout.strip()
    finally:
        os.unlink(tmp_path)


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
#
# Uses SSHSubprocessStore pointed at pynixd as a client. This is
# exactly what nix does: SSH → exec "nix-daemon --stdio" → daemon protocol.


@pytest.mark.asyncio
@pytest.mark.timeout(300)
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
async def test_ssh_serve_small_nars(
    request: pytest.FixtureRequest,
    concurrency: int,
    warm: bool,
) -> None:
    """Benchmark: fetch many small NARs from pynixd over SSH."""
    # Use system store as pynixd's local store
    local_store = LocalSocketStore(id="bench-local", max_transfers=20)

    # Pick paths before starting pynixd (from system store directly)
    picked = await _pick_small_paths(local_store, 500)
    assert len(picked) >= 100, f"Need 100+ small paths, found {len(picked)}"

    async with run_pynixd(
        stores={},
        local_store=local_store,
        client_store_path="/tmp/pynixd-bench-client",
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
            with _profile_context(f"ssh_serve_small_c{concurrency}_{warmth}"):
                start = time.monotonic()
                await asyncio.gather(*[_fetch(p, i.nar_size) for p, i in picked])
                elapsed = time.monotonic() - start
        finally:
            await client.close()

    _record(
        request,
        f"ssh serve small c={concurrency} {warmth}",
        elapsed,
        len(picked),
        total_bytes,
    )


@pytest.mark.asyncio
@pytest.mark.timeout(300)
@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
async def test_ssh_serve_big_nar(
    request: pytest.FixtureRequest,
    warm: bool,
) -> None:
    """Benchmark: fetch a 100MB NAR from pynixd over SSH."""
    store_path = _create_big_path(100)
    local_store = LocalSocketStore(id="bench-local", max_transfers=20)
    info = await local_store.query_path_info(store_path)
    assert info is not None

    async with run_pynixd(
        stores={},
        local_store=local_store,
        client_store_path="/tmp/pynixd-bench-client",
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
    _record(request, f"ssh serve big {warmth}", elapsed, 1, len(data))


@pytest.mark.asyncio
@pytest.mark.timeout(300)
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
async def test_ssh_query_path_info(
    request: pytest.FixtureRequest,
    concurrency: int,
    warm: bool,
) -> None:
    """Benchmark: QueryPathInfo throughput through pynixd over SSH."""
    local_store = LocalSocketStore(id="bench-local", max_transfers=20)

    picked = await _pick_small_paths(local_store, 500)
    assert len(picked) >= 100, f"Need 100+ small paths, found {len(picked)}"

    async with run_pynixd(
        stores={},
        local_store=local_store,
        client_store_path="/tmp/pynixd-bench-client",
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
            with _profile_context(f"ssh_query_c{concurrency}_{warmth}"):
                start = time.monotonic()
                await asyncio.gather(*[_query(p) for p, _ in picked])
                elapsed = time.monotonic() - start
        finally:
            await client.close()

    _record(request, f"ssh query c={concurrency} {warmth}", elapsed, len(picked))


# ── HTTP binary cache benchmarks ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(300)
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
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

    _record(
        request,
        f"http serve small c={concurrency}",
        elapsed,
        len(picked),
        total_bytes,
    )


@pytest.mark.asyncio
@pytest.mark.timeout(300)
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

    _record(request, "http serve big", elapsed, 1, len(data))


@pytest.mark.asyncio
@pytest.mark.timeout(300)
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
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

    _record(request, f"http narinfo c={concurrency}", elapsed, len(picked))


# ── Build dispatch benchmark ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "n_drvs,n_clients",
    # [(10, 1), (50, 1), (50, 5)],
    # ids=["10drv-1cli", "50drv-1cli", "50drv-5cli"],
    [(100, 1)],
    ids=["100drv-1cli"],
)
async def test_build_throughput(
    request: pytest.FixtureRequest,
    nix_env: dict[str, str],
    n_drvs: int,
    n_clients: int,
) -> None:
    """Benchmark: build dispatch throughput with zero-sleep derivations."""
    test_nix = request.config.getoption("--nix")

    stores = make_local_stores(n=4, prefix="bench-build", max_builds=4)

    async with run_pynixd(
        stores,
        njobs=100,
        client_store_path="/tmp/pynixd-bench-build-local",
    ) as server:
        drvs_per_client = n_drvs // n_clients

        async def _run_client(client_id: int) -> float:
            client_store = f"/tmp/pynixd-bench-build-client-{client_id}"
            os.makedirs(client_store, exist_ok=True)
            client_env = nix_env.copy()
            client_env["PYNIXD_PAR_COUNT"] = str(drvs_per_client)
            client_env["PYNIXD_PAR_ID"] = f"b{client_id}"
            client_env["PYNIXD_PAR_SLEEP"] = "0"
            cmd = [
                NIX_BIN,
                "build",
                "--store",
                client_store,
                "--builders",
                server.builder_uri(max_jobs=drvs_per_client),
                "--max-jobs",
                "0",
                "--no-link",
                "--file",
                test_nix,
                "parallel",
            ]
            t0 = time.monotonic()
            rc, _stdout, stderr = _run_subprocess_with_timeout(
                cmd, client_env, timeout=240
            )
            elapsed = time.monotonic() - t0
            if rc != 0:
                log.error("Client %d failed: %s", client_id, stderr[:500])
            assert rc == 0, f"Client {client_id} failed:\n{stderr[:1000]}"
            return elapsed

        with _profile_context(f"build_{n_drvs}drv_{n_clients}cli"):
            start = time.monotonic()
            client_times = await asyncio.gather(
                *[_run_client(i) for i in range(n_clients)]
            )
            total_elapsed = time.monotonic() - start

    _record(
        request,
        f"build {n_drvs}drv {n_clients}cli",
        total_elapsed,
        n_drvs,
    )
    log.info(
        "Build bench: %d drvs, %d clients, %.1fs wall, client times: %s",
        n_drvs,
        n_clients,
        total_elapsed,
        [f"{t:.1f}s" for t in client_times],
    )
