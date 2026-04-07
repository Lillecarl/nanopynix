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
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from conftest import NIX_BIN, _record, rmtree_robust
from environs import Env

from pynixd import wire
from pynixd.operations.base import PathInfo
from pynixd.operations.queries import NarFromPathRequest
from pynixd.store import (
    LocalSocketStore,
    SSHSocketStore,
    SSHSubprocessStore,
    Store,
)
from pynixd.store_path import StorePath

log = structlog.get_logger(__name__)

env = Env()

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
        store_path=BENCH_DST,
        id="bench-dst",
        nix_bin=str(NIX_BIN),
        max_transfers=_MAX_TRANSFERS,
    )
    yield s
    await s.close()


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


def _store_label(store: Store) -> str:
    """Short label for benchmark results based on store type."""
    return store.id


def _set_chunk_size(chunk_kb: int) -> int:
    """Set the wire chunk size and return the previous value."""
    old = wire._CHUNK_SIZE
    wire._CHUNK_SIZE = chunk_kb * 1024
    return old


@pytest.mark.timeout(600)
@pytest.mark.parametrize("chunk_kb", _CHUNK_SIZES_KB)
@pytest.mark.bench
async def test_big_nar_copy_paths(
    request: pytest.FixtureRequest,
    bench_store: Store,
    dst_store: LocalSocketStore,
    chunk_kb: int,
) -> None:
    """Benchmark: stream a 100MB NAR via copy_paths at various chunk sizes."""
    store_path = _create_big_path(100)
    info = await bench_store.query_path_info(store_path)
    assert info is not None

    label = _store_label(bench_store)
    old = _set_chunk_size(chunk_kb)
    try:
        start = time.monotonic()
        await dst_store.stream_paths_with_info_from(bench_store, [store_path])
        elapsed = time.monotonic() - start
    finally:
        wire._CHUNK_SIZE = old

    mb_per_s = (info.nar_size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    _record(
        request,
        f"{label} cp big {chunk_kb}KB",
        elapsed=f"{elapsed:.1f}s",
        throughput=f"{mb_per_s:.1f} MB/s",
    )


@pytest.mark.timeout(600)
@pytest.mark.parametrize("chunk_kb", _CHUNK_SIZES_KB)
@pytest.mark.bench
async def test_big_nar_pipe_nar_from(
    request: pytest.FixtureRequest,
    bench_store: Store,
    dst_store: LocalSocketStore,
    chunk_kb: int,
) -> None:
    """Benchmark: stream a 100MB NAR via pipe_nar_from at various chunk sizes."""
    store_path = _create_big_path(100)
    info = await bench_store.query_path_info(store_path)
    assert info is not None

    label = _store_label(bench_store)
    old = _set_chunk_size(chunk_kb)
    try:
        start = time.monotonic()
        await dst_store.pipe_nar_from(bench_store, store_path, info)
        elapsed = time.monotonic() - start
    finally:
        wire._CHUNK_SIZE = old

    mb_per_s = (info.nar_size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    _record(
        request,
        f"{label} pipe big {chunk_kb}KB",
        elapsed=f"{elapsed:.1f}s",
        throughput=f"{mb_per_s:.1f} MB/s",
    )


@pytest.mark.timeout(600)
@pytest.mark.parametrize("chunk_kb", _CHUNK_SIZES_KB)
@pytest.mark.bench
async def test_small_nars_copy_paths(
    request: pytest.FixtureRequest,
    bench_store: Store,
    dst_store: LocalSocketStore,
    chunk_kb: int,
) -> None:
    """Benchmark: stream many small NARs via copy_paths (batched).

    AddMultipleToStore sends all paths over a single connection pair —
    no per-path connection overhead, no concurrency dimension needed.
    """
    picked = await _pick_small_paths(bench_store, 1000)
    assert len(picked) >= 100, f"Need 100+ small paths, found {len(picked)}"
    total_bytes = sum(info.nar_size for _, info in picked)

    label = _store_label(bench_store)
    old = _set_chunk_size(chunk_kb)
    try:
        start = time.monotonic()
        await dst_store.stream_paths_with_info_from(bench_store, [p for p, _ in picked])
        elapsed = time.monotonic() - start
    finally:
        wire._CHUNK_SIZE = old

    mb_per_s = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    paths_per_s = len(picked) / elapsed if elapsed > 0 else 0
    _record(
        request,
        f"{label} cp small {chunk_kb}KB",
        elapsed=f"{elapsed:.1f}s",
        throughput=f"{mb_per_s:.1f} MB/s",
        ops=f"{paths_per_s:.0f} paths/s",
    )


@pytest.mark.timeout(600)
@pytest.mark.parametrize("chunk_kb", _CHUNK_SIZES_KB)
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
@pytest.mark.bench
async def test_small_nars_pipe_nar_from(
    request: pytest.FixtureRequest,
    bench_store: Store,
    dst_store: LocalSocketStore,
    chunk_kb: int,
    concurrency: int,
) -> None:
    """Benchmark: stream many small NARs via pipe_nar_from.

    Each pipe_nar_from holds one connection on src and one on dst for the
    full transfer. Concurrency spawns multiple connection pairs.
    """
    picked = await _pick_small_paths(bench_store, 1000)
    assert len(picked) >= 100, f"Need 100+ small paths, found {len(picked)}"
    total_bytes = sum(info.nar_size for _, info in picked)

    label = _store_label(bench_store)
    old = _set_chunk_size(chunk_kb)
    try:
        sem = asyncio.Semaphore(concurrency)

        async def _pipe_one(path: str, info: PathInfo) -> None:
            async with sem:
                await dst_store.pipe_nar_from(bench_store, path, info)

        start = time.monotonic()
        await asyncio.gather(*[_pipe_one(p, i) for p, i in picked])
        elapsed = time.monotonic() - start
    finally:
        wire._CHUNK_SIZE = old

    mb_per_s = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    paths_per_s = len(picked) / elapsed if elapsed > 0 else 0
    _record(
        request,
        f"{label} pipe small c={concurrency} {chunk_kb}KB",
        elapsed=f"{elapsed:.1f}s",
        throughput=f"{mb_per_s:.1f} MB/s",
        ops=f"{paths_per_s:.0f} paths/s",
    )


@pytest.mark.timeout(300)
@pytest.mark.bench
async def test_serve_big_nar(
    request: pytest.FixtureRequest,
    bench_store: Store,
) -> None:
    """Benchmark: how fast can we read a 100MB NAR via nar_from_path."""
    store_path = _create_big_path(100)

    label = _store_label(bench_store)
    start = time.monotonic()
    resp = await bench_store.execute(NarFromPathRequest(path=store_path))
    nar_data = resp.nar_data
    elapsed = time.monotonic() - start

    assert len(nar_data) > 0
    mb_per_s = (len(nar_data) / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    _record(
        request,
        f"{label} serve big",
        elapsed=f"{elapsed:.1f}s",
        throughput=f"{mb_per_s:.1f} MB/s",
    )


@pytest.mark.timeout(300)
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
@pytest.mark.bench
async def test_serve_small_nars(
    request: pytest.FixtureRequest,
    bench_store: Store,
    concurrency: int,
) -> None:
    """Benchmark: serve many small NARs via buffer_nar_from_path."""
    picked = await _pick_small_paths(bench_store, 1000)
    assert len(picked) >= 100, f"Need 100+ small paths, found {len(picked)}"

    label = _store_label(bench_store)
    sem = asyncio.Semaphore(concurrency)
    total_bytes = 0
    lock = asyncio.Lock()

    async def _serve_one(path: StorePath, nar_size: int) -> None:
        nonlocal total_bytes
        async with sem:
            resp = await bench_store.execute(
                NarFromPathRequest(path=path, nar_size=nar_size)
            )
            nar_data = resp.nar_data
        async with lock:
            total_bytes += len(nar_data)

    start = time.monotonic()
    await asyncio.gather(*[_serve_one(p, info.nar_size) for p, info in picked])
    elapsed = time.monotonic() - start

    mb_per_s = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    paths_per_s = len(picked) / elapsed if elapsed > 0 else 0
    _record(
        request,
        f"{label} serve small c={concurrency}",
        elapsed=f"{elapsed:.1f}s",
        throughput=f"{mb_per_s:.1f} MB/s",
        ops=f"{paths_per_s:.0f} paths/s",
    )
