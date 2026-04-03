"""Standalone build benchmark test over Unix socket.

Two threads:
- Thread 1: pynixd Unix socket server
- Thread 2: nix build subprocess

Communication: pynixd writes its socket path to a file, main thread reads it
before starting the build thread.

This is identical to test_bench_build.py but uses Unix socket instead of SSH.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from environs import Env

log = logging.getLogger(__name__)
nixclient_log = logging.getLogger("nixclient")

# Silence aiosqlite verbose logging
aiosqlite_logger = logging.getLogger("aiosqlite")
aiosqlite_logger.setLevel(logging.WARNING)

env = Env()

NIX_BIN = env.path("NIX_BIN", Path("nix"))


@dataclass
class BenchResult:
    label: str
    elapsed: float
    count: int
    profile_path: str | None = None


_build_bench_key = pytest.StashKey[list[BenchResult]]()


def _run_pynixd_thread(
    ready_event: threading.Event,
    socket_path: Path,
    stop_event: threading.Event,
) -> None:
    """Run pynixd in a dedicated thread with its own event loop."""
    import pyinstrument

    # Start profiling before asyncio.run() so we capture everything
    profiler = pyinstrument.Profiler(async_mode="enabled")
    profiler.start()

    async def _async_run() -> None:
        from pynixd.instance import PynixdConfig, Server
        from pynixd.store import LocalSocketStore, Store

        local_store = LocalSocketStore(
            id="local",
            store_path=Path("/tmp/pynixd-local-unix"),
            max_builds=0,
            max_transfers=64,
        )
        stores: Mapping[str, Store] = {
            "builder": LocalSocketStore(
                id="builder",
                store_path=Path("/tmp/pynixd-builder-unix"),
                max_builds=100,
                max_transfers=100,
            )
        }

        config = PynixdConfig(
            local_store=local_store,
            stores=stores,
            unix_path=socket_path,
        )

        server = Server(config)
        await server.start()
        ready_event.set()

        while not stop_event.is_set():
            await asyncio.sleep(0.1)

        await server.close()
        await server.wait_finished()

    asyncio.run(_async_run())

    # Stop profiling and write output after event loop ends
    profiler.stop()
    import tempfile

    profile_path = tempfile.mktemp(prefix="/tmp/pynixd-profile-")
    with open(profile_path, "w") as f:
        f.write(profiler.output_text(unicode=True, color=False, show_all=True))
    print(f"Profile written to: {profile_path}")


def _build_in_thread(
    socket_path: Path,
    client_store: Path,
    nix_file: Path,
    target: str,
    stop_event: threading.Event,
) -> float:
    """Run nix build in a dedicated thread."""
    env = os.environ.copy()
    cmd = [
        str(NIX_BIN),
        "build",
        "--store",
        str(client_store),
        "--builders",
        f"unix://{socket_path}?remote-store={socket_path} x86_64-linux - 100",
        "--max-jobs",
        "0",
        "--no-link",
        "--file",
        str(nix_file),
        target,
    ]

    log.info("Starting build: %s", " ".join(cmd))
    start = time.monotonic()
    result = subprocess.run(cmd, env=env, capture_output=True)
    elapsed = time.monotonic() - start

    for line in result.stdout.decode().splitlines():
        nixclient_log.info("%s", line)
    for line in result.stderr.decode().splitlines():
        nixclient_log.info("%s", line)

    if result.returncode != 0:
        log.error("Build failed with rc=%d", result.returncode)

    return elapsed


def test_build_throughput(request: pytest.FixtureRequest) -> None:
    """Run nix build against pynixd Unix server and measure wall time."""
    nix_file = env.path("PYNIXD_TEST_NIX", Path("test.nix"))

    client_store = Path(tempfile.mkdtemp(prefix="pynixd-bench-client-"))
    os.makedirs(client_store, exist_ok=True)

    # Communication: pynixd writes socket path here
    ready_event = threading.Event()
    socket_path = Path(f"/tmp/pynixd-bench-{os.getpid()}.socket")
    if socket_path.exists():
        socket_path.unlink()
    stop_event = threading.Event()

    # Start pynixd thread
    pynixd_thread = threading.Thread(
        target=_run_pynixd_thread,
        args=(ready_event, socket_path, stop_event),
        name="pynixd",
    )
    pynixd_thread.start()

    try:
        # Wait for pynixd to be ready
        if not ready_event.wait(timeout=30):
            raise RuntimeError("pynixd thread failed to become ready")

        elapsed = _build_in_thread(
            socket_path,
            client_store,
            nix_file,
            ".bench-100mb",
            stop_event,
        )
        print(f"\n  Build completed in {elapsed:.1f}s")

        # Record for terminal summary
        results = request.config.stash.get(_build_bench_key, [])
        results.append(
            BenchResult(
                label="Unix Socket (100MB build)",
                elapsed=elapsed,
                count=1,
            )
        )
        request.config.stash[_build_bench_key] = results

    finally:
        stop_event.set()
        pynixd_thread.join(timeout=10)
        if socket_path.exists():
            socket_path.unlink()
        subprocess.run(["rm", "-rf", str(client_store)])
