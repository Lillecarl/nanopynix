"""Dedicated build throughput benchmark with baselines."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pyinstrument
import pytest
import structlog
from pyinstrument.renderers import ConsoleRenderer

from pynixd.instance import PynixdConfig, Server
from pynixd.store import LocalSocketStore
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    _prune_client_processor,
    get_test_store_kwargs,
    rmtree_robust,
    run_logged,
)

log = structlog.get_logger(__name__)

# Common test configuration
NIX_FILE = Path("test.nix")
TARGET = "parallel"
MAX_JOBS = 20
TEST_ENV = {
    "PYNIXD_PAR_COUNT": "100",
    "PYNIXD_PAR_SLEEP": "0",
    "PYNIXD_PAR_ID": "throughput-profile",
}


@pytest.mark.benchmark
@pytest.mark.timeout(120)
async def test_throughput_local() -> None:
    """Baseline: Build directly with a custom local store path."""
    store_path = STORE_PREFIX / "throughput-local-baseline"
    rmtree_robust(store_path)
    store_path.mkdir(parents=True, exist_ok=True)

    log.info("starting_local_baseline")
    cmd = [
        str(NIX_BIN),
        "build",
        "--max-jobs",
        str(MAX_JOBS),
        "--no-link",
        "--file",
        str(NIX_FILE),
        "--store",
        str(store_path),
        TARGET,
    ]

    start = time.perf_counter()
    rc = await run_logged(cmd, env=TEST_ENV)
    elapsed = time.perf_counter() - start

    assert rc == 0, "Local baseline build failed"
    log.info("local_baseline_finished", elapsed=f"{elapsed:.2f}s")


@pytest.mark.benchmark
@pytest.mark.timeout(120)
async def test_throughput_daemon() -> None:
    """Baseline: Build against a standard Nix daemon."""
    store_path = STORE_PREFIX / "throughput-daemon-baseline"
    rmtree_robust(store_path)
    store_path.mkdir(parents=True, exist_ok=True)

    socket_path = store_path / "nix" / "var" / "nix" / "daemon-socket" / "socket"
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    daemon_env = os.environ.copy()
    daemon_env["NIX_DAEMON_SOCKET_PATH"] = str(socket_path)

    log.info("spawning_baseline_daemon", store_path=str(store_path))
    proc = await asyncio.create_subprocess_exec(
        str(NIX_BIN),
        "daemon",
        "--store",
        str(store_path),
        "--max-jobs",
        str(MAX_JOBS),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=daemon_env,
    )

    try:
        # Wait for daemon
        for _ in range(100):
            if socket_path.exists():
                break
            await asyncio.sleep(0.05)

        for _ in range(50):
            try:
                r, w = await asyncio.open_unix_connection(str(socket_path))
                w.close()
                await w.wait_closed()
                break
            except (ConnectionRefusedError, ConnectionResetError):
                await asyncio.sleep(0.1)

        remote_uri = f"unix://{socket_path}"

        log.info("starting_daemon_baseline")
        cmd = [
            str(NIX_BIN),
            "build",
            "--max-jobs",
            str(MAX_JOBS),
            "--no-link",
            "--file",
            str(NIX_FILE),
            "--store",
            "daemon",
            TARGET,
        ]

        start = time.perf_counter()
        rc = await run_logged(cmd, env=TEST_ENV | {"NIX_REMOTE": remote_uri})
        elapsed = time.perf_counter() - start

        assert rc == 0, "Daemon baseline build failed"
        log.info("daemon_baseline_finished", elapsed=f"{elapsed:.2f}s")
    finally:
        proc.terminate()
        await proc.wait()


@pytest.mark.benchmark
async def test_throughput_pynixd(test_log_dir: Path) -> None:
    """pynixd: Build through pynixd proxy."""
    # With MAX_JOBS=20 and sleep=0, builds complete in ~10-30s depending on system.
    async with asyncio.timeout(None):
        local_path = STORE_PREFIX / "throughput-pynixd-local"
        builder_path = STORE_PREFIX / "throughput-pynixd-builder"
        rmtree_robust(local_path)
        rmtree_robust(builder_path)
        local_path.mkdir(parents=True, exist_ok=True)
        builder_path.mkdir(parents=True, exist_ok=True)

        local_store = LocalSocketStore(
            id="local",
            store_path=local_path,
            max_builds=0,
            max_transfers=100,
            **get_test_store_kwargs(),
        )
        builder_store = LocalSocketStore(
            id="builder",
            store_path=builder_path,
            max_builds=MAX_JOBS,
            max_transfers=100,
            **get_test_store_kwargs(),
        )

        socket_path = local_path / "pynixd.socket"
        config = PynixdConfig(
            local_store=local_store,
            stores={"builder": builder_store},
            unix_path=socket_path,
            ssh_port=None,
        )

        async with Server(config) as server:
            log.info("server_up_starting_profiler")
            await server.start()

            profiler = pyinstrument.Profiler(async_mode="enabled")
            profiler.start()

            try:
                # remote_uri = f"unix://{socket_path}"
                # remote_uri = f"unix://{socket_path}?root={local_store.store_path}&real={local_store.store_path / "nix/store"}&state={local_store.store_path / "nix/var"}"
                remote_uri = f"unix://{socket_path}?root={local_store.store_path}"

                cmd = [
                    str(NIX_BIN),
                    "build",
                    "--max-jobs",
                    str(MAX_JOBS),
                    "--no-link",
                    "--file",
                    str(NIX_FILE),
                    "--store",
                    remote_uri,
                    "--eval-store",
                    remote_uri,
                    # "--store",
                    # str(local_store.store_path),
                    # "--store",
                    # remote_uri,
                    # "--eval-store",
                    # remote_uri,
                    # "--store",
                    # str(STORE_PREFIX / "client-build"),
                    # "--eval-store",
                    # str(remote_uri),
                    # "--builders",
                    # f"{remote_uri} x86_64-linux - 1",
                    TARGET,
                ]

                start = time.perf_counter()
                rc = await run_logged(
                    cmd,
                    env=TEST_ENV
                    | {
                        # "NIX_REMOTE": remote_uri,
                        # "NIX_DATA_DIR": str(local_store.store_path / "nix/share"),
                        # "NIX_CONF_DIR": str(local_store.store_path / "nix/etc/nix"),
                        # "NIX_LOG_DIR": str(local_store.store_path / "nix/var/log"),
                        # "NIX_STATE_DIR": str(local_store.store_path / "nix/var"),
                        # "NIX_STORE_DIR": str(local_store.store_path / "nix/store"),
                    },
                )
                elapsed = time.perf_counter() - start

                assert rc == 0, "pynixd throughput build failed"
                log.info("pynixd_throughput_finished", elapsed=f"{elapsed:.2f}s")
            finally:
                profiler.stop()
                log.info("build_finished_stopping_profiler")

                session = profiler.last_session
                if session:
                    renderer = ConsoleRenderer(unicode=True, color=False, show_all=True)
                    renderer.processors.insert(0, _prune_client_processor)
                    profile_path = test_log_dir / "pyinstrument"
                    with open(profile_path, "w") as f:
                        f.write(renderer.render(session))
                    log.info("profile_saved", path=str(profile_path))

                await server.close()
