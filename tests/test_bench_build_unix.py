"""Standalone build benchmark test over Unix socket.

This test runs both the pynixd Unix socket server and the nix build client
within the same asyncio event loop. A custom pyinstrument processor is used
to prune the client-side subprocess execution from the profile, so the
resulting profile only reflects server performance.
"""

from __future__ import annotations

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
        if child.function and "_run_build_async" in child.function:
            # Prune this child and all its descendants
            child.remove_from_parent()
        else:
            _prune_client_processor(child, options)

    return frame


async def _run_build_async(
    socket_path: Path,
    nix_file: Path,
    target: str,
) -> float:
    """Run nix build asynchronously."""
    build_env = os.environ.copy()
    build_env["NIX_REMOTE"] = f"unix://{socket_path}"

    cmd = [
        str(NIX_BIN),
        "build",
        "--max-jobs",
        "100",
        "--no-link",
        "--file",
        str(nix_file),
        target,
    ]

    log.info("Starting build: %s", " ".join(cmd))
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
    """Run nix build against pynixd Unix server and measure wall time."""
    caplog.set_level(logging.INFO)
    nix_file = env.path("PYNIXD_TEST_NIX", Path("test.nix"))

    socket_path = Path(f"/tmp/pynixd-bench-{os.getpid()}.socket")
    if socket_path.exists():
        socket_path.unlink()

    local_store = LocalSocketStore(
        id="local",
        store_path=Path("/tmp/pynixd-local-unix"),
        max_builds=0,
        max_transfers=100,
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

    profiler = pyinstrument.Profiler(async_mode="enabled")
    profiler.start()

    try:
        elapsed = await _run_build_async(
            socket_path,
            nix_file,
            "bench-100mb",
        )
        print(f"\n  Build completed in {elapsed:.1f}s")
    finally:
        profiler.stop()

        await server.close()
        await server.wait_finished()

        if socket_path.exists():
            socket_path.unlink()

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
        print("No profile session captured")

    # Record for terminal summary
    results = request.config.stash.get(_build_bench_key, [])
    results.append(
        BenchResult(
            label="Unix Socket (100MB build)",
            elapsed=elapsed,
            count=1,
            profile_path=profile_path,
        )
    )
    request.config.stash[_build_bench_key] = results
