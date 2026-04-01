"""Standalone build benchmark test.

Two threads:
- Thread 1: pynixd SSH server
- Thread 2: nix build subprocess

Communication: pynixd writes its bound port to a file, main thread reads it
before starting the build thread.
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

log = logging.getLogger(__name__)

NIX_BIN = os.environ.get("NIX_BIN", "nix")


@dataclass
class BenchResult:
    label: str
    elapsed: float
    count: int


def _run_pynixd_thread(
    ready_event: threading.Event,
    port_holder: list[int],
    stop_event: threading.Event,
) -> None:
    """Run pynixd in a dedicated thread with its own event loop."""
    import pyinstrument

    # Start profiling before asyncio.run() so we capture everything
    profiler = pyinstrument.Profiler(async_mode="enabled")
    profiler.start()

    async def _async_run() -> None:
        from pynixd.ssh_server import start_ssh_server
        from pynixd.store import LocalSocketStore
        from pynixd.build_queue import BuildQueue
        from pynixd.scheduler import Scheduler
        from pynixd.local_store_db import LocalStoreDB
        from pynixd.gc import GarbageCollector

        local_store = LocalSocketStore(
            id="local", store_path="/tmp/pynixd-local", max_builds=0, max_transfers=64
        )
        stores = {
            "builder": LocalSocketStore(
                id="builder",
                store_path="/tmp/pynixd-builder",
                max_builds=100,
                max_transfers=100,
            )
        }

        # Shared resources
        build_queue = BuildQueue()
        scheduler = Scheduler(build_queue, stores, local_store)

        # Initialize local store and backends
        await local_store.probe_version()
        local_store.db = await LocalStoreDB.open(local_store.store_path or "/")

        for store in stores.values():
            try:
                await store.sync_paths()
            except Exception:
                log.exception("Failed to sync paths for store %s", store.id)

        # Start background services
        scheduler_task = asyncio.create_task(scheduler.start())
        gc: GarbageCollector | None = None
        if local_store.db is not None:
            local_store.db.start()
            gc = GarbageCollector(local_store.db, stores, local_store)
            gc.start()

        ssh_server = await start_ssh_server(
            stores=stores,
            local_store=local_store,
            build_queue=build_queue,
            scheduler=scheduler,
            host="127.0.0.1",
            port=0,
        )
        port_holder.append(ssh_server.get_port())
        ready_event.set()

        try:
            while not stop_event.is_set():
                await asyncio.sleep(0.1)
        finally:
            ssh_server.close()
            await ssh_server.wait_closed()
            scheduler_task.cancel()
            if gc:
                await gc.stop()
            if local_store.db:
                await local_store.db.close()
            await local_store.close()
            for store in stores.values():
                await store.close()

    asyncio.run(_async_run())

    # Stop profiling and write output after event loop ends
    profiler.stop()
    import tempfile

    profile_path = tempfile.mktemp(prefix="/tmp/pynixd-profile-")
    with open(profile_path, "w") as f:
        f.write(profiler.output_text(unicode=True, color=False, show_all=True))
    print(f"Profile written to: {profile_path}")


def _build_in_thread(
    port: int,
    client_store: str,
    nix_file: str,
    target: str,
    stop_event: threading.Event,
) -> float:
    """Run nix build in a dedicated thread."""
    username = os.environ.get("USER", "root")
    builder_uri = f"ssh-ng://{username}@127.0.0.1:{port} x86_64-linux - 100"

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
    result = subprocess.run(cmd, env=env)
    elapsed = time.monotonic() - start

    if result.returncode != 0:
        log.error("Build failed with rc=%d", result.returncode)

    return elapsed


def test_build_throughput() -> None:
    """Run nix build against pynixd and measure wall time."""
    nix_file = os.environ.get("PYNIXD_TEST_NIX", "test.nix")

    client_store = tempfile.mkdtemp(prefix="pynixd-bench-client-")
    os.makedirs(client_store, exist_ok=True)

    # Communication: pynixd writes port here
    ready_event = threading.Event()
    port_holder: list[int] = []
    stop_event = threading.Event()

    # Start pynixd thread
    pynixd_thread = threading.Thread(
        target=_run_pynixd_thread,
        args=(ready_event, port_holder, stop_event),
        name="pynixd",
        daemon=True,
    )
    pynixd_thread.start()

    # Wait for pynixd to be ready
    ready_event.wait(timeout=10)
    if not port_holder:
        raise RuntimeError("pynixd did not expose port")

    port = port_holder[0]
    log.info("pynixd ready on port %d", port)

    elapsed = _build_in_thread(port, client_store, nix_file, "parallel", stop_event)

    # Signal pynixd to stop so profiler output is printed
    stop_event.set()
    pynixd_thread.join(timeout=10)

    result = BenchResult(
        label="build 100drv 1cli",
        elapsed=elapsed,
        count=100,
    )
    log.info("Result: %s", result)
