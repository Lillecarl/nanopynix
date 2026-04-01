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
from dataclasses import dataclass

import pytest

log = logging.getLogger(__name__)
nixclient_log = logging.getLogger("nixclient")

# Silence aiosqlite verbose logging
aiosqlite_logger = logging.getLogger("aiosqlite")
aiosqlite_logger.setLevel(logging.WARNING)

NIX_BIN = os.environ.get("NIX_BIN", "nix")


@dataclass
class BenchResult:
    label: str
    elapsed: float
    count: int
    profile_path: str = ""


# Stash key for build bench results
_build_bench_key = pytest.StashKey[list[BenchResult]]()


def _run_pynixd_thread(
    ready_event: threading.Event,
    socket_path: str,
    stop_event: threading.Event,
    profile_holder: list[str],
) -> None:
    """Run pynixd Unix socket server in a dedicated thread with its own event loop."""
    import pyinstrument

    # Start profiling before asyncio.run() so we capture everything
    profiler = pyinstrument.Profiler(async_mode="enabled")
    profiler.start()

    async def _async_run() -> None:
        from pynixd.instance import PynixdConfig, run_pynixd
        from pynixd.store import LocalSocketStore

        local_store = LocalSocketStore(
            id="local", store_path="/", max_builds=0, max_transfers=64
        )
        stores = {
            "builder": LocalSocketStore(
                id="builder",
                store_path="/tmp/pynixd-builder",
                max_builds=100,
                max_transfers=100,
            )
        }

        config = PynixdConfig(
            local_store=local_store,
            stores=stores,
            unix_path=socket_path,
        )

        # Run pynixd until stop_event is set
        run_task = asyncio.create_task(run_pynixd(config, ready_event=ready_event))
        
        while not stop_event.is_set():
            await asyncio.sleep(0.1)
        
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_async_run())

    # Stop profiling and write output after event loop ends
    profiler.stop()
    # Use fixed path so main thread can read it
    profile_path = "/tmp/pynixd-build-bench-profile.txt"
    with open(profile_path, "w") as f:
        f.write(profiler.output_text(unicode=True, color=False, show_all=True))
    profile_holder.append(profile_path)


def _build_in_thread(
    socket_path: str,
    client_store: str,
    nix_file: str,
    target: str,
) -> float:
    """Run nix build in a dedicated thread."""
    builder_uri = f"unix://{socket_path} x86_64-linux - 100"

    env = os.environ.copy()
    env["NIX_SSHOPTS"] = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

    cmd = [
        NIX_BIN,
        "build",
        "--store",
        client_store,
        "--builders",
        builder_uri,
        "--max-jobs",
        "0",
        "--no-link",
        "--file",
        nix_file,
        target,
    ]

    log.info("Starting build: %s", " ".join(cmd))
    start = time.monotonic()
    result = subprocess.run(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
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
    nix_file = os.environ.get("PYNIXD_TEST_NIX", "test.nix")

    client_store = tempfile.mkdtemp(prefix="pynixd-bench-client-")
    os.makedirs(client_store, exist_ok=True)

    # Communication: pynixd writes socket path here
    ready_event = threading.Event()
    socket_path = "/tmp/pynixd-bench-unix.sock"
    stop_event = threading.Event()
    profile_holder: list[str] = []

    # Start pynixd thread
    pynixd_thread = threading.Thread(
        target=_run_pynixd_thread,
        args=(ready_event, socket_path, stop_event, profile_holder),
        name="pynixd",
        daemon=True,
    )
    pynixd_thread.start()

    # Wait for pynixd to be ready
    ready_event.wait(timeout=10)

    log.info("pynixd ready on socket %s", socket_path)

    elapsed = _build_in_thread(socket_path, client_store, nix_file, "parallel")

    # Signal pynixd to stop so profiler output is printed
    stop_event.set()
    pynixd_thread.join(timeout=30)

    profile_path = profile_holder[0] if profile_holder else ""

    result = BenchResult(
        label="build 100drv 1cli (unix)",
        elapsed=elapsed,
        count=9999999,
        profile_path=profile_path,
    )
    log.info("Result: %s", result)

    # Store in pytest stash for summary
    results = request.config.stash.setdefault(_build_bench_key, [])
    results.append(result)
