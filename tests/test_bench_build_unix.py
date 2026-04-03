"""Standalone build benchmark test over Unix socket.

This test runs both the pynixd Unix socket server and the nix build client
within the same asyncio event loop. A custom pyinstrument processor is used
to prune the client-side subprocess execution from the profile, so the
resulting profile only reflects server performance.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pyinstrument
import pytest
from conftest import (
    NIX_BIN,
    rmtree_robust,
    run_process_async,
)
from environs import Env
from pyinstrument.renderers import ConsoleRenderer

from pynixd.instance import PynixdConfig, Server
from pynixd.store import LocalSocketStore, Store

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)
nixclient_log = logging.getLogger("nixclient")

# Silence aiosqlite verbose logging
aiosqlite_logger = logging.getLogger("aiosqlite")
aiosqlite_logger.setLevel(logging.WARNING)

env = Env()


@dataclass
class BenchResult:
    label: str
    elapsed: float
    baselines: dict[str, float]
    count: int
    profile_path: str | None = None


_build_bench_key = pytest.StashKey[list[BenchResult]]()


def _prune_client_processor(frame, options):
    """Custom pyinstrument processor to remove client-side execution.

    Prunes the client-side subprocess execution from the profile.
    """
    if frame is None:
        return None

    # We must iterate over a copy of children because remove_from_parent mutates
    # the list.
    for child in list(frame.children):
        if child.function and "run_nix_build" in child.function:
            # Prune this child and all its descendants
            child.remove_from_parent()
        else:
            _prune_client_processor(child, options)

    return frame


async def run_nix_build(
    nix_file: Path,
    target: str,
    remote: str | None = None,
    store: str | None = "daemon",
) -> float:
    """Run nix build and return elapsed time in seconds."""
    build_env = os.environ.copy()
    if remote:
        build_env["NIX_REMOTE"] = remote

    cmd = [
        str(NIX_BIN),
        "build",
        "--max-jobs",
        "100",
        "--no-link",
        "--file",
        str(nix_file),
    ]
    if store:
        cmd.extend(["--store", store])
    cmd.append(target)

    log.info("Starting build: %s (NIX_REMOTE=%s)", " ".join(cmd), remote)
    start = time.monotonic()
    rc, stdout, stderr = await run_process_async(cmd, env=build_env)
    elapsed = time.monotonic() - start

    for line in stdout.splitlines():
        nixclient_log.info("%s", line)
    for line in stderr.splitlines():
        nixclient_log.info("%s", line)

    if rc != 0:
        log.error("Build failed with rc=%d", rc)
        msg = f"Build failed with rc={rc}"
        raise RuntimeError(msg)

    return elapsed


@pytest.mark.asyncio
async def test_build_throughput(
    request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture
) -> None:
    """Benchmark pynixd build throughput against multiple baselines."""
    caplog.set_level(logging.INFO)
    nix_file = env.path("PYNIXD_TEST_NIX", Path("test.nix"))
    target = "bench-100mb"
    baselines: dict[str, float] = {}

    # 1. Baseline: Local Store (No Daemon)
    # This uses direct DB access, no protocol overhead at all.
    local_store_path = Path("/tmp/pynixd-baseline-local")
    rmtree_robust(local_store_path)
    local_store_path.mkdir(parents=True, exist_ok=True)
    try:
        print(f"\n  Running baseline (Local Store, No Daemon) in {local_store_path}...")
        baselines["no-daemon"] = await run_nix_build(
            nix_file, target, store=str(local_store_path)
        )
        print(f"  Completed in {baselines['no-daemon']:.1f}s")
    finally:
        rmtree_robust(local_store_path)

    # 2. Baseline: Nix Daemon
    # This uses the standard C++ daemon protocol.
    daemon_store_path = Path("/tmp/pynixd-baseline-daemon")
    rmtree_robust(daemon_store_path)
    daemon_store_path.mkdir(parents=True, exist_ok=True)

    baseline_socket = daemon_store_path / "nix" / "daemon-socket" / "socket"
    baseline_socket.parent.mkdir(parents=True, exist_ok=True)

    print(f"  Spawning baseline Nix daemon in {daemon_store_path}...")
    baseline_env = os.environ.copy()
    baseline_env["NIX_DAEMON_SOCKET_PATH"] = str(baseline_socket)

    daemon_proc = await asyncio.create_subprocess_exec(
        str(NIX_BIN),
        "daemon",
        "--store",
        str(daemon_store_path),
        "--max-jobs",
        "100",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=baseline_env,
    )

    try:
        # Wait for socket
        for _ in range(100):
            if baseline_socket.exists():
                break
            await asyncio.sleep(0.05)

        # Probe connection
        for _ in range(50):
            try:
                r, w = await asyncio.open_unix_connection(str(baseline_socket))
                w.close()
                await w.wait_closed()
                break
            except (ConnectionRefusedError, ConnectionResetError):
                await asyncio.sleep(0.1)

        print("  Running baseline (Nix Daemon)...")
        baselines["daemon"] = await run_nix_build(
            nix_file, target, remote=f"unix://{baseline_socket}"
        )
        print(f"  Completed in {baselines['daemon']:.1f}s")
    finally:
        daemon_proc.terminate()
        await daemon_proc.wait()
        rmtree_robust(daemon_store_path)

    # 3. pynixd build
    socket_path = Path(f"/tmp/pynixd-bench-{os.getpid()}.socket")
    if socket_path.exists():
        socket_path.unlink()

    pynixd_local_path = Path("/tmp/pynixd-bench-local")
    pynixd_builder_path = Path("/tmp/pynixd-bench-builder")
    rmtree_robust(pynixd_local_path)
    rmtree_robust(pynixd_builder_path)
    pynixd_local_path.mkdir(parents=True, exist_ok=True)
    pynixd_builder_path.mkdir(parents=True, exist_ok=True)

    try:
        local_store = LocalSocketStore(
            id="local",
            store_path=pynixd_local_path,
            max_builds=0,
            max_transfers=100,
        )
        stores: Mapping[str, Store] = {
            "builder": LocalSocketStore(
                id="builder",
                store_path=pynixd_builder_path,
                max_builds=100,
                max_transfers=100,
                extra_args=["--max-jobs", "100"],
            )
        }

        config = PynixdConfig(
            local_store=local_store,
            stores=stores,
            unix_path=socket_path,
        )

        server = Server(config)
        await server.start()

        profiler = pyinstrument.Profiler(async_mode="enabled")
        profiler.start()

        try:
            print("  Running pynixd build...")
            elapsed = await run_nix_build(nix_file, target, remote=f"unix://{socket_path}")
            print(f"  Completed in {elapsed:.1f}s")
        finally:
            profiler.stop()
            await server.close()
            await server.wait_finished()
            if socket_path.exists():
                socket_path.unlink()
    finally:
        rmtree_robust(pynixd_local_path)
        rmtree_robust(pynixd_builder_path)

    # Output profile
    session = profiler.last_session
    if session:
        renderer = ConsoleRenderer(unicode=True, color=False, show_all=True)
        renderer.processors.insert(0, _prune_client_processor)
        profile_path = tempfile.mktemp(prefix="/tmp/pynixd-profile-")
        with open(profile_path, "w") as f:
            f.write(renderer.render(session))
        print(f"Profile written to: {profile_path}")
    else:
        profile_path = None

    # Record for terminal summary
    results = request.config.stash.get(_build_bench_key, [])
    results.append(
        BenchResult(
            label="Unix Socket (100MB build)",
            elapsed=elapsed,
            baselines=baselines,
            count=1,
            profile_path=profile_path,
        )
    )
    request.config.stash[_build_bench_key] = results
