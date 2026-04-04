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
from dataclasses import dataclass
from pathlib import Path

import pytest
import structlog
from conftest import NIX_BIN, rmtree_robust
from environs import Env

from pynixd import wire
from pynixd.operations.base import PathInfo
from pynixd.store import (
    LocalSocketStore,
    LocalSubprocessStore,
    SSHSocketStore,
    SSHSubprocessStore,
    Store,
)

log = structlog.get_logger(__name__)

env = Env()

pytestmark = pytest.mark.benchmark

BENCH_DST = Path("/tmp/pynixd-bench-dst")

_SSH_USER = env.str("USER", "root")

_CHUNK_SIZES_KB = env.list("PYNIXD_BENCH_CHUNKS", [64, 256, 1024, 4096], subcast=int)

# Store types that can read from the system store (used as NAR source).
# local-subprocess is excluded — it has its own isolated store and can't
# see paths created via "nix store add" on the system store.
_STORE_TYPES = ["local-socket", "ssh-subprocess", "ssh-socket"]

# Concurrency capped at 8 to stay within OpenSSH MaxSessions=10 default
# (each nix-daemon --stdio channel consumes one SSH session).
_CONCURRENCY_LEVELS = [1, 4, 8]

# Transfer pool size — enough for max concurrency on both src and dst
_MAX_TRANSFERS = 10


# ── Result collection ────────────────────────────────────────────


@dataclass
class BenchResult:
    label: str
    chunk_kb: int
    elapsed: float
    total_bytes: int
    count: int

    @property
    def mb(self) -> float:
        return self.total_bytes / (1024 * 1024)

    @property
    def mb_per_s(self) -> float:
        return self.mb / self.elapsed if self.elapsed > 0 else 0

    @property
    def paths_per_s(self) -> float:
        return self.count / self.elapsed if self.elapsed > 0 else 0


def _record(
    request: pytest.FixtureRequest,
    label: str,
    chunk_kb: int,
    elapsed: float,
    total_bytes: int,
    count: int,
) -> None:
    r = BenchResult(label, chunk_kb, elapsed, total_bytes, count)
    # Stash on the session config so conftest can find it
    results = request.config.stash.setdefault(_bench_results_key, [])
    results.append(r)


_bench_results_key = pytest.StashKey[list[BenchResult]]()


# ── Store factory ────────────────────────────────────────────────


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


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture(params=_STORE_TYPES)
async def bench_store(request: pytest.FixtureRequest) -> AsyncIterator[Store]:
    """Parametrized store fixture — yields one store per type."""
    s = await _make_store(request.param)
    yield s
    await s.close()


@pytest.fixture
async def dst_store() -> AsyncIterator[LocalSubprocessStore]:
    rmtree_robust(BENCH_DST)
    os.makedirs(BENCH_DST, exist_ok=True)
    s = LocalSubprocessStore(
        store_path=BENCH_DST,
        id="bench-dst",
        nix_bin=str(NIX_BIN),
        max_transfers=_MAX_TRANSFERS,
    )
    yield s
    await s.close()


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


def _store_label(store: Store) -> str:
    """Short label for benchmark results based on store type."""
    return store.id


def _set_chunk_size(chunk_kb: int) -> int:
    """Set the wire chunk size and return the previous value."""
    old = wire._CHUNK_SIZE
    wire._CHUNK_SIZE = chunk_kb * 1024
    return old


# ── Big NAR benchmark ────────────────────────────────────────────


@pytest.mark.timeout(600)
@pytest.mark.parametrize("chunk_kb", _CHUNK_SIZES_KB)
@pytest.mark.bench
async def test_big_nar_copy_paths(
    request: pytest.FixtureRequest,
    bench_store: Store,
    dst_store: LocalSubprocessStore,
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
        await dst_store.stream_paths_store_to_store(bench_store, [(store_path, info)])
        elapsed = time.monotonic() - start
    finally:
        wire._CHUNK_SIZE = old

    _record(request, f"{label} cp big", chunk_kb, elapsed, info.nar_size, 1)


@pytest.mark.timeout(600)
@pytest.mark.parametrize("chunk_kb", _CHUNK_SIZES_KB)
@pytest.mark.bench
async def test_big_nar_pipe_nar_from(
    request: pytest.FixtureRequest,
    bench_store: Store,
    dst_store: LocalSubprocessStore,
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

    _record(request, f"{label} pipe big", chunk_kb, elapsed, info.nar_size, 1)


# ── Many small NARs benchmark ────────────────────────────────────


@pytest.mark.timeout(600)
@pytest.mark.parametrize("chunk_kb", _CHUNK_SIZES_KB)
@pytest.mark.bench
async def test_small_nars_copy_paths(
    request: pytest.FixtureRequest,
    bench_store: Store,
    dst_store: LocalSubprocessStore,
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
        await dst_store.stream_paths_store_to_store(bench_store, picked)
        elapsed = time.monotonic() - start
    finally:
        wire._CHUNK_SIZE = old

    _record(request, f"{label} cp small", chunk_kb, elapsed, total_bytes, len(picked))


@pytest.mark.timeout(600)
@pytest.mark.parametrize("chunk_kb", _CHUNK_SIZES_KB)
@pytest.mark.parametrize("concurrency", _CONCURRENCY_LEVELS)
@pytest.mark.bench
async def test_small_nars_pipe_nar_from(
    request: pytest.FixtureRequest,
    bench_store: Store,
    dst_store: LocalSubprocessStore,
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

    _record(
        request,
        f"{label} pipe small c={concurrency}",
        chunk_kb,
        elapsed,
        total_bytes,
        len(picked),
    )


# ── NAR serving benchmark (read from store) ──────────────────────


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
    nar_data = await bench_store.buffer_nar_from_path(store_path)
    elapsed = time.monotonic() - start

    assert len(nar_data) > 0
    _record(request, f"{label} serve big", 0, elapsed, len(nar_data), 1)


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

    async def _serve_one(path: str, nar_size: int) -> None:
        nonlocal total_bytes
        async with sem:
            nar_data = await bench_store.buffer_nar_from_path(path, nar_size=nar_size)
        async with lock:
            total_bytes += len(nar_data)

    start = time.monotonic()
    await asyncio.gather(*[_serve_one(p, info.nar_size) for p, info in picked])
    elapsed = time.monotonic() - start

    _record(
        request,
        f"{label} serve small c={concurrency}",
        0,
        elapsed,
        total_bytes,
        len(picked),
    )
