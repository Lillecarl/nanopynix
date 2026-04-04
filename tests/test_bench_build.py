"""Standalone build benchmark test.

Two threads:
- Thread 1: pynixd SSH server
- Thread 2: nix build subprocess

Communication: pynixd writes its bound port to a file, main thread reads it
before starting the build thread.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
import structlog
from environs import Env

log = structlog.get_logger(__name__)

env = Env()

NIX_BIN = env.path("NIX_BIN", Path("nix"))


@dataclass
class BenchResult:
    label: str
    elapsed: float
    count: int


def _run_server_thread(
    ready_event: threading.Event,
    port: int,
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
            store_path=Path("/tmp/pynixd-local"),
            max_builds=0,
            max_transfers=64,
        )
        stores: Mapping[str, Store] = {
            "builder": LocalSocketStore(
                id="builder",
                store_path=Path("/tmp/pynixd-builder"),
                max_builds=100,
                max_transfers=100,
            )
        }

        config = PynixdConfig(
            local_store=local_store,
            stores=stores,
            ssh_host="127.0.0.1",
            ssh_port=port,
        )

        try:
            server = Server(config)
            await server.start()
            ready_event.set()

            while not stop_event.is_set():
                await asyncio.sleep(0.1)

            await server.close()
            await server.wait_finished()
        except Exception as e:
            print(f"Exception in _async_run: {e}")
            import traceback

            traceback.print_exc()
            stop_event.set()
            ready_event.set()

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
    client_store: Path,
    nix_file: Path,
    target: str,
    stop_event: threading.Event,
) -> float:
    """Run nix build in a dedicated thread."""
    username = env.str("USER", "root")
    builder_uri = f"ssh-ng://{username}@127.0.0.1:{port} x86_64-linux - 100"

    sub_env = os.environ.copy()
    sub_env["NIX_SSHOPTS"] = (
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    )

    cmd = [
        str(NIX_BIN),
        "build",
        "--store",
        str(client_store),
        "--builders",
        builder_uri,
        "--max-jobs",
        "0",
        "--no-link",
        "--file",
        str(nix_file),
        target,
    ]

    log.info("starting_build", cmd=" ".join(cmd))
    start = time.monotonic()
    result = subprocess.run(cmd, env=sub_env, capture_output=True)
    elapsed = time.monotonic() - start

    if result.returncode != 0:
        log.error(
            "build_failed",
            rc=result.returncode,
            stdout=result.stdout.decode(),
            stderr=result.stderr.decode(),
        )
        msg = f"Build failed with rc={result.returncode}"
        raise RuntimeError(msg)

    return elapsed


@pytest.mark.bench
def test_build_throughput() -> None:
    """Run nix build against pynixd and measure wall time."""
    from conftest import get_free_port

    nix_file = env.path("PYNIXD_TEST_NIX", Path("test.nix"))

    client_store = Path(tempfile.mkdtemp(prefix="pynixd-bench-client-"))
    os.makedirs(client_store, exist_ok=True)

    # Communication: pynixd writes port here
    ready_event = threading.Event()
    port = get_free_port()
    stop_event = threading.Event()

    # Start pynixd thread
    pynixd_thread = threading.Thread(
        target=_run_server_thread,
        args=(ready_event, port, stop_event),
        name="pynixd",
    )
    pynixd_thread.start()

    try:
        # Wait for pynixd to be ready
        if not ready_event.wait(timeout=30):
            raise RuntimeError("pynixd thread failed to become ready")

        elapsed = _build_in_thread(
            port,
            client_store,
            nix_file,
            "bench-100mb",
            stop_event,
        )
        print(f"\n  Build completed in {elapsed:.1f}s")

    finally:
        stop_event.set()
        pynixd_thread.join(timeout=10)
        subprocess.run(["rm", "-rf", client_store])
