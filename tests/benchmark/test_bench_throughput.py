"""Dedicated build throughput benchmark for profiling."""

from __future__ import annotations

from pathlib import Path

import pyinstrument
import pytest
import structlog
from pyinstrument.renderers import ConsoleRenderer

from pynixd.instance import PynixdConfig, Server
from pynixd.store import LocalSocketStore
from tests.benchmark.test_bench_build import run_nix_build
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    _prune_client_processor,
    get_test_store_kwargs,
    rmtree_robust,
)

log = structlog.get_logger(__name__)


@pytest.mark.benchmark
@pytest.mark.timeout(120)
async def test_throughput_profiling(test_log_dir: Path) -> None:
    """Build 100 zero-sleep derivations and profile pynixd overhead."""
    nix_file = Path("test.nix")
    target = "parallel"
    max_jobs = 100

    # 100 derivations, 0 sleep
    test_env = {
        "PYNIXD_PAR_COUNT": "100",
        "PYNIXD_PAR_SLEEP": "0",
        "PYNIXD_PAR_ID": "throughput-profile",
    }

    local_path = STORE_PREFIX / "throughput-local"
    builder_path = STORE_PREFIX / "throughput-builder"
    rmtree_robust(local_path)
    rmtree_robust(builder_path)
    local_path.mkdir(parents=True, exist_ok=True)
    builder_path.mkdir(parents=True, exist_ok=True)

    local_store = LocalSocketStore(
        id="local",
        store_path=local_path,
        max_builds=0,  # force remote build
        **get_test_store_kwargs(),
    )
    builder_store = LocalSocketStore(
        id="builder",
        store_path=builder_path,
        max_builds=max_jobs,
        **get_test_store_kwargs(),
    )

    config = PynixdConfig(
        local_store=local_store,
        stores={"builder": builder_store},
        ssh_host="127.0.0.1",
        ssh_port=0,
    )

    async with Server(config) as server:
        log.info("server_up_starting_profiler")

        profiler = pyinstrument.Profiler(async_mode="enabled")
        profiler.start()

        try:
            # We use the builder URI directly to simulate a remote client
            username = "lillecarl"
            remote = f"ssh-ng://{username}@127.0.0.1:{server.port}"

            await run_nix_build(
                nix_file,
                target,
                max_jobs=max_jobs,
                client_bin=Path(NIX_BIN),
                remote=remote,
                extra_env=test_env,
            )
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
