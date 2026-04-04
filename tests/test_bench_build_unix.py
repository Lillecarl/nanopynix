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
import random
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pyinstrument
import pytest
from conftest import (
    LIX_BIN,
    NIX_BIN,
    rmtree_robust,
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

CLIENT_BINS: list[tuple[Path, str]] = [
    (NIX_BIN, "nix"),
    (LIX_BIN, "lix"),
]


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
    max_jobs: int,
    client_bin: Path,
    remote: str | None = None,
    store: str | None = "daemon",
    extra_env: dict[str, str] | None = None,
) -> float:
    """Run nix build and return elapsed time in seconds."""
    build_env = os.environ.copy()
    if remote:
        build_env["NIX_REMOTE"] = remote
    if extra_env:
        build_env.update(extra_env)

    cmd = [
        str(client_bin),
        "build",
        "--max-jobs",
        str(max_jobs),
        "--no-link",
        "--file",
        str(nix_file),
    ]
    if store:
        cmd.extend(["--store", store])
    cmd.append(target)

    log.info("Starting build: %s (NIX_REMOTE=%s)", " ".join(cmd), remote)
    start = time.perf_counter()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=build_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _stream(stream: asyncio.StreamReader) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            nixclient_log.info("%s", line.decode().rstrip())

    assert proc.stdout is not None
    assert proc.stderr is not None
    await asyncio.gather(
        _stream(proc.stdout),
        _stream(proc.stderr),
    )

    rc = await proc.wait()
    elapsed = time.perf_counter() - start

    if rc != 0:
        log.error("Build failed with rc=%d", rc)
        msg = f"Build failed with rc={rc}"
        raise RuntimeError(msg)

    return elapsed


@pytest.mark.parametrize("client_bin,client_label", CLIENT_BINS)
@pytest.mark.parametrize("max_jobs", [10, 100])
@pytest.mark.parametrize("sleep_secs", [0, 1])
async def test_build_throughput(
    request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture,
    client_bin: Path,
    client_label: str,
    max_jobs: int,
    sleep_secs: int,
) -> None:
    """Benchmark pynixd build throughput against multiple baselines."""
    caplog.set_level(logging.INFO)
    nix_file = env.path("PYNIXD_TEST_NIX", Path("test.nix"))
    target = "parallel"

    # Configure test.nix via environment variables
    test_nix_env = {
        "PYNIXD_PAR_COUNT": "100",  # Always 100 drvs, just varies max-jobs
        "PYNIXD_PAR_SLEEP": str(sleep_secs),
        "PYNIXD_PAR_ID": f"bench-{max_jobs}-{sleep_secs}",
    }

    results_accumulator: dict[str, list[float]] = {
        "no-daemon": [],
        "daemon": [],
        "pynixd": [],
    }
    last_profile_path: str | None = None

    async def run_no_daemon() -> float:
        # 1. Baseline: Local Store (No Daemon)
        local_store_path = Path(
            f"/tmp/pynixd-baseline-local-{client_label}-{max_jobs}-{sleep_secs}"
        )
        rmtree_robust(local_store_path)
        local_store_path.mkdir(parents=True, exist_ok=True)
        try:
            log.info(
                "Running baseline (Local Store, No Daemon) in %s...", local_store_path
            )
            elapsed = await run_nix_build(
                nix_file,
                target,
                max_jobs=max_jobs,
                client_bin=client_bin,
                store=str(local_store_path),
                extra_env=test_nix_env,
            )
            log.info("Completed in %.1fs", elapsed)
            return elapsed
        finally:
            rmtree_robust(local_store_path)

    async def run_daemon() -> float:
        # 2. Baseline: Nix Daemon
        daemon_store_path = Path(
            f"/tmp/pynixd-baseline-daemon-{client_label}-{max_jobs}-{sleep_secs}"
        )
        rmtree_robust(daemon_store_path)
        daemon_store_path.mkdir(parents=True, exist_ok=True)

        baseline_socket = daemon_store_path / "nix" / "daemon-socket" / "socket"
        baseline_socket.parent.mkdir(parents=True, exist_ok=True)

        log.info(
            "Spawning baseline %s daemon in %s...", client_label, daemon_store_path
        )
        baseline_env = os.environ.copy()
        baseline_env["NIX_DAEMON_SOCKET_PATH"] = str(baseline_socket)

        daemon_proc = await asyncio.create_subprocess_exec(
            str(client_bin),
            "daemon",
            "--store",
            str(daemon_store_path),
            "--max-jobs",
            str(max_jobs),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=baseline_env,
        )

        try:
            for _ in range(100):
                if baseline_socket.exists():
                    break
                await asyncio.sleep(0.05)

            for _ in range(50):
                try:
                    r, w = await asyncio.open_unix_connection(str(baseline_socket))
                    w.close()
                    await w.wait_closed()
                    break
                except (ConnectionRefusedError, ConnectionResetError):
                    await asyncio.sleep(0.1)

            log.info("Running baseline (%s Daemon, jobs=%d)...", client_label, max_jobs)
            elapsed = await run_nix_build(
                nix_file,
                target,
                max_jobs=max_jobs,
                client_bin=client_bin,
                remote=f"unix://{baseline_socket}",
                store="daemon",
                extra_env=test_nix_env,
            )
            log.info("Completed in %.1fs", elapsed)
            return elapsed
        finally:
            daemon_proc.terminate()
            await daemon_proc.wait()
            rmtree_robust(daemon_store_path)

    async def run_pynixd() -> float:
        nonlocal last_profile_path
        # 3. pynixd build
        pid = os.getpid()
        socket_file = (
            f"/tmp/pynixd-bench-{client_label}-{pid}-{max_jobs}-{sleep_secs}.socket"
        )
        socket_path = Path(socket_file)
        if socket_path.exists():
            socket_path.unlink()

        pynixd_local_path = Path(
            f"/tmp/pynixd-bench-local-{client_label}-{max_jobs}-{sleep_secs}"
        )
        pynixd_builder_path = Path(
            f"/tmp/pynixd-bench-builder-{client_label}-{max_jobs}-{sleep_secs}"
        )
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
                    max_builds=max_jobs,
                    max_transfers=max_jobs,
                    nix_bin=str(client_bin),
                    extra_args=["--max-jobs", str(max_jobs)],
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
                log.info(
                    "Running pynixd build (client=%s, jobs=%d)...",
                    client_label,
                    max_jobs,
                )
                elapsed = await run_nix_build(
                    nix_file,
                    target,
                    max_jobs=max_jobs,
                    client_bin=client_bin,
                    remote=f"unix://{socket_path}",
                    store="daemon",
                    extra_env=test_nix_env,
                )
                log.info("Completed in %.1fs", elapsed)
                return elapsed
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
                    fd, profile_path = tempfile.mkstemp(
                        prefix="pynixd-profile-", suffix=".txt"
                    )
                    os.close(fd)
                    with open(profile_path, "w") as f:
                        f.write(renderer.render(session))
                    last_profile_path = profile_path
        finally:
            rmtree_robust(pynixd_local_path)
            rmtree_robust(pynixd_builder_path)

    tasks: list[tuple[str, Callable[[], Awaitable[float]]]] = [
        ("no-daemon", run_no_daemon),
        ("daemon", run_daemon),
        ("pynixd", run_pynixd),
    ]

    iterations = env.int("PYNIXD_BENCH_ITERATIONS", 1)
    for i in range(iterations):
        if iterations > 1:
            log.info(
                "\n--- Iteration %d/%d (jobs=%d, sleep=%d) ---",
                i + 1,
                iterations,
                max_jobs,
                sleep_secs,
            )

        # Shuffle to mitigate page cache order bias
        current_tasks = list(tasks)
        random.shuffle(current_tasks)

        for name, task in current_tasks:
            elapsed = await task()
            results_accumulator[name].append(elapsed)

    # Average results
    pynixd_times = results_accumulator["pynixd"]
    final_elapsed = sum(pynixd_times) / len(pynixd_times)
    final_baselines = {
        name: sum(times) / len(times)
        for name, times in results_accumulator.items()
        if name != "pynixd"
    }

    # Record for terminal summary
    results = request.config.stash.get(_build_bench_key, [])
    label = (
        f"Unix Socket ({client_label}, jobs={max_jobs}, "
        f"sleep={sleep_secs}s, {iterations} iters)"
    )
    results.append(
        BenchResult(
            label=label,
            elapsed=final_elapsed,
            baselines=final_baselines,
            count=1,
            profile_path=last_profile_path,
        )
    )
    request.config.stash[_build_bench_key] = results
