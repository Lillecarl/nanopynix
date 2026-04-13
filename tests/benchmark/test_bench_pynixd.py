"""pynixd performance benchmarks.

Measures latency and throughput for various Nix daemon operations
across different store types.

Store types are parametrized so each test runs against:
- local-socket: system daemon Unix socket
- ssh-subprocess: SSH channel -> nix-daemon --stdio (localhost)
- ssh-socket: SSH tunnel -> daemon Unix socket (localhost)

The benchmarks use the system Nix store as the source of paths.
Results are recorded via the conftest.py record_bench helper.
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

from pynixd.operations.is_valid_path import IsValidPathRequest
from pynixd.operations.query_all_valid_paths import QueryAllValidPathsRequest
from pynixd.operations.query_path_info import QueryPathInfoRequest
from pynixd.store import (
    LocalSocketStore,
    SSHSocketStore,
    SSHSubprocessStore,
    Store,
)
from pynixd.store_path import StorePath

log = structlog.get_logger(__name__)


_SSH_USER = env.str("USER", "root")

_STORE_TYPES = ["local-socket", "ssh-socket"]

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


async def _get_test_paths(n: int = 100) -> list[StorePath]:
    """Get n arbitrary valid paths from the system store."""
    out = subprocess.check_output(
        [NIX_BIN, "query", "--all", "--limit", str(n)], text=True
    )
    return [StorePath(p) for p in out.splitlines() if p.strip()]


@pytest.mark.benchmark
async def test_bench_query_all_valid_paths(bench_store: Store) -> None:
    """Measure latency of QueryAllValidPaths."""
    start = time.perf_counter()
    resp = await bench_store.execute(QueryAllValidPathsRequest())
    elapsed = time.perf_counter() - start

    _record(
        "query_all_valid_paths",
        store=bench_store.id,
        count=len(resp.paths),
        latency_ms=elapsed * 1000,
    )


@pytest.mark.benchmark
async def test_bench_query_path_info_latency(bench_store: Store) -> None:
    """Measure latency of QueryPathInfo for 100 random paths."""
    paths = await _get_test_paths(100)

    latencies = []
    for path in paths:
        start = time.perf_counter()
        await bench_store.execute(QueryPathInfoRequest(path=path))
        latencies.append(time.perf_counter() - start)

    avg_latency = sum(latencies) / len(latencies)
    _record(
        "query_path_info_latency",
        store=bench_store.id,
        avg_ms=avg_latency * 1000,
        p95_ms=sorted(latencies)[int(len(latencies) * 0.95)] * 1000,
    )


@pytest.mark.benchmark
async def test_bench_is_valid_path_throughput(bench_store: Store) -> None:
    """Measure throughput of IsValidPath (ops/s)."""
    paths = await _get_test_paths(500)

    start = time.perf_counter()
    # Execute sequentially to measure overhead
    for path in paths:
        await bench_store.execute(IsValidPathRequest(path=path))
    elapsed = time.perf_counter() - start

    ops_per_s = len(paths) / elapsed
    _record(
        "is_valid_path_throughput",
        store=bench_store.id,
        ops_per_s=ops_per_s,
    )


@pytest.mark.benchmark
async def test_bench_is_valid_path_parallel(bench_store: Store) -> None:
    """Measure throughput of IsValidPath with 10 parallel tasks."""
    paths = await _get_test_paths(1000)

    start = time.perf_counter()
    tasks = [bench_store.execute(IsValidPathRequest(path=path)) for path in paths]
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - start

    ops_per_s = len(paths) / elapsed
    _record(
        "is_valid_path_parallel",
        store=bench_store.id,
        parallel_tasks=10,
        ops_per_s=ops_per_s,
    )


@pytest.mark.benchmark
async def test_bench_local_socket_overhead() -> None:
    """Compare direct Unix socket vs pynixd LocalSocketStore."""
    path = (await _get_test_paths(1))[0]

    # 1. Direct system socket
    system_store = LocalSocketStore(id="system")
    latencies_direct = []
    for _ in range(100):
        start = time.perf_counter()
        await system_store.execute(QueryPathInfoRequest(path=path))
        latencies_direct.append(time.perf_counter() - start)
    await system_store.close()

    # 2. Managed socket (spawned daemon)
    managed_path = Path("/tmp/pynixd-bench-managed")
    rmtree_robust(managed_path)
    os.makedirs(managed_path, exist_ok=True)
    managed_store = LocalSocketStore(
        id="managed",
        store_path=managed_path,
    )
    # Managed store will be empty, so we just measure the handshake + op overhead
    latencies_managed = []
    for _ in range(100):
        start = time.perf_counter()
        await managed_store.execute(QueryPathInfoRequest(path=path))
        latencies_managed.append(time.perf_counter() - start)
    await managed_store.close()

    _record(
        "local_overhead",
        direct_avg_ms=(sum(latencies_direct) / 100) * 1000,
        managed_avg_ms=(sum(latencies_managed) / 100) * 1000,
    )
