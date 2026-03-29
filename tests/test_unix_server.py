"""Tests using pynixd as a Unix socket daemon replacing the nix daemon.

pynixd listens on a Unix socket. The client's nix connects to it directly
(via --store unix:///path/to/socket) instead of the system daemon. pynixd
uses the host's daemon socket as its local store and two LocalSocketStore
instances with temp paths as "remote" builders.

The key test is test_build_parallel_comparison: builds 100 parallel
derivations both through pynixd and directly via nix, comparing wall-clock
times to measure pynixd's overhead as a daemon replacement.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time

import pytest
from conftest import (
    NIX_BIN,
    _get_system,
    _run_subprocess_with_timeout,
)

from pynixd.store import LocalSocketStore, Store
from pynixd.unix_server import run_unix_server

log = logging.getLogger(__name__)

# Silence noisy per-op loggers
logging.getLogger("pynixd.op.AddToStore").setLevel(logging.WARNING)

SOCKET_PATH = "/tmp/pynixd-test-unix/pynixd.sock"
PAR_COUNT = 100


async def _run_pynixd_unix(
    stores: dict[str, Store],
    local_store: Store,
    socket_path: str = SOCKET_PATH,
    ready_event: asyncio.Event | None = None,
) -> None:
    """Run pynixd unix server (meant to be wrapped in a task)."""
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    await run_unix_server(
        stores=stores,
        local_store=local_store,
        socket_path=socket_path,
        ready_event=ready_event,
    )


async def _nix_build_unix(
    socket_path: str,
    env: dict[str, str],
    *extra_args: str,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Run nix build using pynixd unix socket as the store."""
    store_uri = f"unix://{socket_path}"
    cmd = [
        NIX_BIN,
        "build",
        "--store",
        store_uri,
        "--no-link",
        *extra_args,
    ]
    log.info("nix_build_unix: %s", " ".join(shlex.quote(a) for a in cmd))
    return await _run_subprocess_with_timeout(cmd, env, timeout)


async def _nix_build_direct(
    store_path: str,
    env: dict[str, str],
    *extra_args: str,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Run nix build directly against a local store (no pynixd)."""
    cmd = [
        NIX_BIN,
        "build",
        "--store",
        store_path,
        "--no-link",
        "--max-jobs",
        "150",
        *extra_args,
    ]
    log.info("nix_build_direct: %s", " ".join(shlex.quote(a) for a in cmd))
    return await _run_subprocess_with_timeout(cmd, env, timeout)


async def _nix_store_unix(
    socket_path: str,
    env: dict[str, str],
    *args: str,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run nix store subcommand against pynixd unix socket."""
    store_uri = f"unix://{socket_path}"
    cmd = [
        NIX_BIN,
        "store",
        *args,
        "--store",
        store_uri,
    ]
    log.info("nix_store_unix: %s", " ".join(shlex.quote(a) for a in cmd))
    return await _run_subprocess_with_timeout(cmd, env, timeout)


@pytest.fixture
def local_store() -> LocalSocketStore:
    """Host's system daemon as pynixd's local store."""
    return LocalSocketStore(
        id="local-system",
        store_path="/",
        max_builds=0,
        max_transfers=64,
        nix_bin=NIX_BIN,
    )


@pytest.fixture
def builder_stores() -> dict[str, Store]:
    """Single builder using the host's system store — same as local_store."""
    store = LocalSocketStore(
        id="builder",
        store_path="/",
        max_builds=150,
        max_transfers=64,
        supported_systems=[_get_system()],
        nix_bin=NIX_BIN,
    )
    return {store.id: store}


# ── Store query tests ────────────────────────────────────────────────


@pytest.mark.store
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_store_ping(
    nix_env: dict[str, str],
    local_store: LocalSocketStore,
    builder_stores: dict[str, Store],
) -> None:
    """Verify nix can connect to pynixd unix socket and run store ping."""
    ready = asyncio.Event()
    task = asyncio.create_task(
        _run_pynixd_unix(builder_stores, local_store, ready_event=ready)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)

        rc, stdout, stderr = await _nix_store_unix(
            SOCKET_PATH,
            nix_env,
            "ping",
        )
        log.info("store ping: rc=%d stdout=%s stderr=%s", rc, stdout, stderr[:500])
        assert rc == 0, f"nix store ping failed: {stderr}"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for s in builder_stores.values():
            await s.close()
        await local_store.close()


# ── Build tests ──────────────────────────────────────────────────────


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_build_parallel_comparison(
    nix_env: dict[str, str],
    local_store: LocalSocketStore,
    builder_stores: dict[str, Store],
    request: pytest.FixtureRequest,
) -> None:
    """Build 100 parallel derivations: pynixd unix socket vs direct nix.

    Runs the same build twice:
    1. Through pynixd unix socket (pynixd distributes across 2 builders)
    2. Directly via nix against a temp store (nix builds locally)

    Compares wall-clock times to measure pynixd overhead and whether
    nix sends derivations serially over unix sockets (like ssh-ng).
    """
    test_nix = request.config.getoption("--nix")
    direct_store = "/tmp/pynixd-test-unix-direct"
    os.makedirs(direct_store, exist_ok=True)

    build_env = nix_env.copy()
    build_env["PYNIXD_PAR_COUNT"] = str(PAR_COUNT)

    # ── Run 1: through pynixd ────────────────────────────────────────

    ready = asyncio.Event()
    task = asyncio.create_task(
        _run_pynixd_unix(builder_stores, local_store, ready_event=ready)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)

        t0 = time.monotonic()
        rc_pynixd, _, stderr_pynixd = await _nix_build_unix(
            SOCKET_PATH,
            build_env,
            "--file",
            test_nix,
            "parallel",
            timeout=300,
        )
        elapsed_pynixd = time.monotonic() - t0
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for s in builder_stores.values():
            await s.close()
        await local_store.close()

    assert rc_pynixd == 0, f"pynixd build failed:\n{stderr_pynixd}"

    # ── Run 2: direct nix build (fresh drvs via unique env) ──────────

    # Use a different PAR_ID so derivations are unique (builtins.currentTime
    # already ensures this, but belt-and-suspenders)
    direct_env = build_env.copy()
    direct_env["PYNIXD_PAR_ID"] = "direct"

    t0 = time.monotonic()
    rc_direct, _, stderr_direct = await _nix_build_direct(
        direct_store,
        direct_env,
        "--file",
        test_nix,
        "parallel",
        timeout=300,
    )
    elapsed_direct = time.monotonic() - t0

    assert rc_direct == 0, f"direct build failed:\n{stderr_direct}"

    # ── Compare ──────────────────────────────────────────────────────

    n_slots_pynixd = sum(s._build_semaphore._value for s in builder_stores.values())  # type: ignore[attr-defined]
    print(
        f"\n  === Parallel {PAR_COUNT} × 2s derivations ==="
        f"\n  pynixd (unix socket, 2 builders): {elapsed_pynixd:.1f}s"
        f"\n  direct (nix, local store):         {elapsed_direct:.1f}s"
        f"\n  ratio (pynixd / direct):           {elapsed_pynixd / elapsed_direct:.2f}x"
        f"\n  pynixd builder slots:              {n_slots_pynixd}"
    )
