"""Standalone build benchmark test.

Runs both the pynixd server and the nix build client within the same
asyncio event loop. A custom pyinstrument processor prunes the client-side
subprocess execution from the profile so the resulting profile only reflects
server performance.

Supports both Unix socket and SSH server modes via parameterization.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pyinstrument
import pytest
import structlog
from conftest import (
    LIX_BIN,
    NIX_BIN,
    _make_profile_filename,
    _prune_client_processor,
    _record,
    rmtree_robust,
)
from environs import Env
from pyinstrument.renderers import ConsoleRenderer

from pynixd.instance import PynixdConfig, Server
from pynixd.store import LocalSocketStore, Store

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)
nixclient_log = structlog.get_logger("nixclient")

aiosqlite_logger = logging.getLogger("aiosqlite")
aiosqlite_logger.setLevel(logging.WARNING)

env = Env()

CLIENT_BINS: list[tuple[Path, str]] = [
    (NIX_BIN, "nix"),
    (LIX_BIN, "lix"),
]


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

    log.info("starting_build", cmd=" ".join(cmd), nix_remote=remote)
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
            nixclient_log.info("nix_client_output", line=line.decode().rstrip())

    assert proc.stdout is not None
    assert proc.stderr is not None
    await asyncio.gather(
        _stream(proc.stdout),
        _stream(proc.stderr),
    )

    rc = await proc.wait()
    elapsed = time.perf_counter() - start

    if rc != 0:
        log.error("build_failed", rc=rc)
        msg = f"Build failed with rc={rc}"
        raise RuntimeError(msg)

    return elapsed


async def run_no_daemon(
    nix_file: Path,
    target: str,
    max_jobs: int,
    client_bin: Path,
    client_label: str,
    sleep_secs: int,
    extra_env: dict[str, str] | None = None,
) -> float:
    """Baseline: Local Store (No Daemon)."""
    local_store_path = Path(
        f"/tmp/pynixd-baseline-local-{client_label}-{max_jobs}-{sleep_secs}"
    )
    rmtree_robust(local_store_path)
    local_store_path.mkdir(parents=True, exist_ok=True)
    try:
        log.info("running_baseline_local_store", store_path=str(local_store_path))
        elapsed = await run_nix_build(
            nix_file,
            target,
            max_jobs=max_jobs,
            client_bin=client_bin,
            store=str(local_store_path),
            extra_env=extra_env,
        )
        log.info("build_completed", elapsed=elapsed)
        return elapsed
    finally:
        rmtree_robust(local_store_path)


async def run_daemon(
    nix_file: Path,
    target: str,
    max_jobs: int,
    client_bin: Path,
    client_label: str,
    sleep_secs: int,
    extra_env: dict[str, str] | None = None,
) -> float:
    """Baseline: Nix Daemon."""
    daemon_store_path = Path(
        f"/tmp/pynixd-baseline-daemon-{client_label}-{max_jobs}-{sleep_secs}"
    )
    rmtree_robust(daemon_store_path)
    daemon_store_path.mkdir(parents=True, exist_ok=True)

    baseline_socket = daemon_store_path / "nix" / "daemon-socket" / "socket"
    baseline_socket.parent.mkdir(parents=True, exist_ok=True)

    log.info(
        "spawning_baseline_daemon",
        client=client_label,
        store_path=str(daemon_store_path),
    )
    baseline_env = os.environ.copy()
    baseline_env["NIX_DAEMON_SOCKET_PATH"] = str(baseline_socket)
    if extra_env:
        baseline_env.update(extra_env)

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

        log.info("running_baseline_daemon", client=client_label, jobs=max_jobs)
        elapsed = await run_nix_build(
            nix_file,
            target,
            max_jobs=max_jobs,
            client_bin=client_bin,
            remote=f"unix://{baseline_socket}",
            store="daemon",
            extra_env=extra_env,
        )
        log.info("build_completed", elapsed=elapsed)
        return elapsed
    finally:
        daemon_proc.terminate()
        await daemon_proc.wait()
        rmtree_robust(daemon_store_path)


async def run_pynixd(
    nix_file: Path,
    target: str,
    max_jobs: int,
    client_bin: Path,
    client_label: str,
    sleep_secs: int,
    server_type: str,
    request: pytest.FixtureRequest,
    extra_env: dict[str, str] | None = None,
) -> tuple[float, str | None]:
    """pynixd build with profiling."""
    pid = os.getpid()
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

    socket_path: Path | None = None

    try:
        local_store = LocalSocketStore(
            id="local",
            store_path=pynixd_local_path,
            max_builds=0,
            max_transfers=100,
        )
        stores: dict[str, Store] = {
            "builder": LocalSocketStore(
                id="builder",
                store_path=pynixd_builder_path,
                max_builds=max_jobs,
                max_transfers=max_jobs,
                nix_bin=str(client_bin),
                extra_args=["--max-jobs", str(max_jobs)],
            )
        }

        if server_type == "unix":
            socket_file = (
                f"/tmp/pynixd-bench-{client_label}-{pid}-{max_jobs}-{sleep_secs}.socket"
            )
            socket_path = Path(socket_file)
            if socket_path.exists():
                socket_path.unlink()
            config = PynixdConfig(
                local_store=local_store,
                stores=stores,
                unix_path=socket_path,
            )
        else:
            config = PynixdConfig(
                local_store=local_store,
                stores=stores,
                ssh_host="127.0.0.1",
                ssh_port=0,
            )

        server = Server(config)
        await server.start()

        profiler = pyinstrument.Profiler(async_mode="enabled")
        profiler.start()

        try:
            if server_type == "unix":
                remote = f"unix://{socket_path}"
            else:
                username = env.str("USER", "root")
                remote = f"ssh-ng://{username}@127.0.0.1:{server.port}"

            log.info(
                "running_pynixd_build",
                server_type=server_type,
                client=client_label,
                jobs=max_jobs,
            )
            elapsed = await run_nix_build(
                nix_file,
                target,
                max_jobs=max_jobs,
                client_bin=client_bin,
                remote=remote,
                store="daemon",
                extra_env=extra_env,
            )
            log.info("build_completed", elapsed=elapsed)
        finally:
            profiler.stop()
            await server.close()
            await server.wait_finished()
            if socket_path and socket_path.exists():
                socket_path.unlink()

        profile_path: str | None = None
        session = profiler.last_session
        if session:
            renderer = ConsoleRenderer(unicode=True, color=False, show_all=True)
            renderer.processors.insert(0, _prune_client_processor)
            fd, profile_path = tempfile.mkstemp(
                prefix=_make_profile_filename(request),
                suffix=".txt",
                dir="/tmp",
            )
            os.close(fd)
            with open(profile_path, "w") as f:
                f.write(renderer.render(session))

        return elapsed, profile_path

    finally:
        rmtree_robust(pynixd_local_path)
        rmtree_robust(pynixd_builder_path)


@pytest.mark.parametrize("server_type", ["unix", "ssh"])
@pytest.mark.parametrize("client_bin,client_label", CLIENT_BINS)
@pytest.mark.parametrize("max_jobs", [10, 100])
@pytest.mark.parametrize("sleep_secs", [0, 1])
@pytest.mark.bench
async def test_build_throughput(
    request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture,
    server_type: str,
    client_bin: Path,
    client_label: str,
    max_jobs: int,
    sleep_secs: int,
) -> None:
    """Benchmark pynixd build throughput against multiple baselines."""
    caplog.set_level(logging.INFO)
    nix_file = env.path("PYNIXD_TEST_NIX", Path("test.nix"))
    target = "parallel"

    test_nix_env = {
        "PYNIXD_PAR_COUNT": "100",
        "PYNIXD_PAR_SLEEP": str(sleep_secs),
        "PYNIXD_PAR_ID": f"bench-{max_jobs}-{sleep_secs}",
    }

    results_accumulator: dict[str, list[float]] = {
        "no-daemon": [],
        "daemon": [],
        "pynixd": [],
    }
    last_profile_path: str | None = None

    tasks: list[tuple[str, Callable[[], Awaitable[float]]]] = [
        (
            "no-daemon",
            lambda env=test_nix_env: run_no_daemon(
                nix_file, target, max_jobs, client_bin, client_label, sleep_secs, env
            ),
        ),
        (
            "daemon",
            lambda env=test_nix_env: run_daemon(
                nix_file, target, max_jobs, client_bin, client_label, sleep_secs, env
            ),
        ),
    ]

    iterations = env.int("PYNIXD_BENCH_ITERATIONS", 1)
    for i in range(iterations):
        if iterations > 1:
            log.info(
                "bench_iteration",
                iteration=i + 1,
                total=iterations,
                jobs=max_jobs,
                sleep=sleep_secs,
            )

        current_tasks = list(tasks)
        random.shuffle(current_tasks)

        for name, task in current_tasks:
            elapsed = await task()
            results_accumulator[name].append(elapsed)

        pynixd_elapsed, profile_path = await run_pynixd(
            nix_file,
            target,
            max_jobs,
            client_bin,
            client_label,
            sleep_secs,
            server_type,
            request,
            test_nix_env,
        )
        results_accumulator["pynixd"].append(pynixd_elapsed)
        last_profile_path = profile_path

    pynixd_times = results_accumulator["pynixd"]
    final_elapsed = sum(pynixd_times) / len(pynixd_times)
    final_baselines: dict[str, str] = {}
    for name, times in results_accumulator.items():
        if name != "pynixd":
            base_avg = sum(times) / len(times)
            overhead = ((final_elapsed / base_avg) - 1) * 100
            final_baselines[name] = f"{base_avg:.1f}s ({overhead:+.1f}%)"

    label = f"build {server_type} {client_label} jobs={max_jobs} sleep={sleep_secs}s"
    _record(
        request,
        label,
        elapsed=f"{final_elapsed:.1f}s",
        baselines=final_baselines,
        profile_path=last_profile_path,
    )
