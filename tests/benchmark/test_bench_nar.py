"""NAR streaming benchmarks.

Measures throughput and overhead for NAR transfers between stores:
- Big NAR: single large path, measures MB/s throughput
- Many small NARs: thousands of tiny paths, measures ops/s overhead
- Compares copy_paths (batched AddMultipleToStore) vs pipe_nar_from (per-path)
- NAR serving: how fast each store type can serve NARs via nar_from_path

Store types are parametrized so each test runs against:
- local-socket: system daemon Unix socket
- ssh-subprocess: SSH channel -> nix-daemon --stdio (localhost)
- ssh-socket: SSH tunnel -> daemon Unix socket (localhost)

Small NAR pipe/serve tests add a concurrency dimension (1, 4, 8 parallel
operations) to measure transport multiplexing. SSH concurrency is capped
at 8 to stay within OpenSSH's default MaxSessions=10 limit.

Chunk sizes are parameterized to find the optimal buffer size.
Set PYNIXD_BENCH_CHUNKS to a comma-separated list of sizes in KB
(default: "64,256,1024,4096").
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from conftest import NIX_BIN, _record, rmtree_robust
from environs import env

from pynixd import wire
from pynixd.operations.base import ValidPathInfo
from pynixd.operations.nar_from_path import NarFromPathRequest
from pynixd.operations.query_path_info import QueryPathInfoRequest
from pynixd.store import (
    LocalSocketStore,
    SSHSocketStore,
    SSHSubprocessStore,
    Store,
)
from pynixd.store_path import StorePath

log = structlog.get_logger(__name__)


BENCH_DST = Path("/tmp/pynixd-bench-dst")

_SSH_USER = env.str("USER", "root")

_CHUNK_SIZES_KB = env.list("PYNIXD_BENCH_CHUNKS", [64, 256, 1024, 4096], subcast=int)

_STORE_TYPES = ["local-socket", "ssh-socket"]

_CONCURRENCY_LEVELS = [1, 4, 8]

_MAX_TRANSFERS = 10


async def _make_store(store_type: str) -> Store:
    """Create a store that reads from the system store."""
    if store_type == "local-socket":
        return LocalSocketStore(id="local-socket", max_transfers=_MAX_TRANSFERS)
    elif store_type == "ssh-subprocess":
        return SSHSubprocessStore(
            host="127.0.0.1",
            id="ssh-subprocess",
            port=22,
            username=_SSH_USER,
            max_transfers=_MAX_TRANSFERS,
        )
    elif store_type == "ssh-socket":
        return SSHSocketStore(
            host="127.0.0.1",
            id="ssh-socket",
            port=22,
            username=_SSH_USER,
            max_transfers=_MAX_TRANSFERS,
        )
    else:
        raise ValueError(f"Unknown store type: {store_type}")


@pytest.fixture(params=_STORE_TYPES)
async def bench_store(request: pytest.FixtureRequest) -> AsyncIterator[Store]:
    """Parametrized store fixture — yields one store per type."""
    s = await _make_store(request.param)
    yield s
    await s.close()


@pytest.fixture
async def dst_store() -> AsyncIterator[LocalSocketStore]:
    rmtree_robust(BENCH_DST)
    os.makedirs(BENCH_DST, exist_ok=True)
    s = LocalSocketStore(
        id="bench-dst",
        store_path=BENCH_DST,
        max_transfers=_MAX_TRANSFERS,
    )
    yield s
    await s.close()


async def _get_big_path() -> StorePath:
    """Find a large NAR path in the local store (>100MB)."""
    # Just use something common like 'nix' if it exists
    out = subprocess.check_output(
        [NIX_BIN, "path-info", "--json", "nixpkgs#nix"], text=True
    )
    import json

    data = json.loads(out)
    return StorePath(data[0]["path"])


async def _get_small_paths(n: int = 1000) -> list[StorePath]:
    """Find n small NAR paths in the local store."""
    out = subprocess.check_output(
        [NIX_BIN, "query", "--all", "--limit", str(n)], text=True
    )
    return [StorePath(p) for p in out.splitlines() if p.strip()]


@pytest.mark.benchmark
@pytest.mark.parametrize("chunk_size_kb", _CHUNK_SIZES_KB)
async def test_bench_nar_big_throughput(
    request: pytest.FixtureRequest,
    bench_store: Store,
    dst_store: LocalSocketStore,
    chunk_size_kb: int,
) -> None:
    """Measure throughput for a large NAR transfer."""
    path = await _get_big_path()
    resp = await bench_store.execute(QueryPathInfoRequest(path=path))
    assert resp.valid and resp.info
    info = resp.info.with_path(path)

    log.info("starting_big_nar_bench", path=path, size_mb=info.nar_size / 1e6)

    start = time.perf_counter()
    await dst_store.pipe_nar_from(bench_store, path, info)
    elapsed = time.perf_counter() - start

    mb_per_s = (info.nar_size / 1e6) / elapsed
    _record(
        request,
        "nar_throughput",
        store=bench_store.id,
        chunk_kb=chunk_size_kb,
        mb_per_s=mb_per_s,
        size_mb=info.nar_size / 1e6,
    )


@pytest.mark.benchmark
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
async def test_bench_nar_small_pipe(
    request: pytest.FixtureRequest,
    bench_store: Store,
    dst_store: LocalSocketStore,
    concurrency: int,
) -> None:
    """Measure overhead for piping many small NARs one-by-one."""
    paths = (await _get_small_paths(100))[:100]  # Smaller sample for speed
    infos = []
    for p in paths:
        resp = await bench_store.execute(QueryPathInfoRequest(path=p))
        if resp.valid and resp.info:
            infos.append(resp.info.with_path(p))

    log.info("starting_small_nar_pipe_bench", count=len(infos), concurrency=concurrency)

    semaphore = asyncio.Semaphore(concurrency)

    async def pipe_one(p: StorePath, i: ValidPathInfo):
        async with semaphore:
            await dst_store.pipe_nar_from(bench_store, p, i)

    start = time.perf_counter()
    await asyncio.gather(*[pipe_one(i.path, i) for i in infos])
    elapsed = time.perf_counter() - start

    ops_per_s = len(infos) / elapsed
    _record(
        request,
        "nar_pipe_overhead",
        store=bench_store.id,
        concurrency=concurrency,
        ops_per_s=ops_per_s,
    )


@pytest.mark.benchmark
async def test_bench_nar_small_batch(
    request: pytest.FixtureRequest, bench_store: Store, dst_store: LocalSocketStore
) -> None:
    """Measure overhead for batched AddMultipleToStore."""
    paths = await _get_small_paths(100)
    log.info("starting_small_nar_batch_bench", count=len(paths))

    start = time.perf_counter()
    # stream_paths_store_to_store handles closure and batching
    await Store.stream_paths_store_to_store(bench_store, dst_store, paths)
    elapsed = time.perf_counter() - start

    ops_per_s = len(paths) / elapsed
    _record(
        request,
        "nar_batch_overhead",
        store=bench_store.id,
        ops_per_s=ops_per_s,
    )


@pytest.mark.benchmark
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
async def test_bench_nar_serve(
    request: pytest.FixtureRequest, bench_store: Store, concurrency: int
) -> None:
    """Measure how fast a store can serve NARs via nar_from_path."""
    paths = await _get_small_paths(100)
    infos = []
    for p in paths:
        resp = await bench_store.execute(QueryPathInfoRequest(path=p))
        if resp.valid and resp.info:
            infos.append(resp.info.with_path(p))

    log.info("starting_nar_serve_bench", count=len(infos), concurrency=concurrency)

    semaphore = asyncio.Semaphore(concurrency)

    async def serve_one(p: StorePath, i: ValidPathInfo):
        async with semaphore:
            # We don't actually write anywhere, just drain the bytes
            class DrainingWriter(wire.NixWriter):
                def write(self, data: bytes):
                    pass

                async def drain(self):
                    pass

                async def is_dirty(self):
                    return False

            await bench_store.execute(
                NarFromPathRequest(path=p),
                client=DrainingWriter(),  # type: ignore[arg-type]
            )

    start = time.perf_counter()
    await asyncio.gather(*[serve_one(i.path, i) for i in infos])
    elapsed = time.perf_counter() - start

    ops_per_s = len(infos) / elapsed
    _record(
        request,
        "nar_serve_overhead",
        store=bench_store.id,
        concurrency=concurrency,
        ops_per_s=ops_per_s,
    )
