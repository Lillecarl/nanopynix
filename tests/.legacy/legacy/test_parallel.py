"""Pressure tests for build parallelism.

Verifies that pynixd correctly limits concurrency based on max_builds,
queues extra builds, and handles multiple concurrent clients build requests
without overwhelming backends.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
import structlog
from conftest import (
    LIX_BIN,
    NIX_BIN,
    make_local_stores,
    nix_command,
)

from pynixd import Server
from pynixd.instance import NixImplementation
from pynixd.store import LocalSocketStore

log = structlog.get_logger(__name__)


@pytest.mark.parallel
@pytest.mark.builders
@pytest.mark.timeout(300)
@pytest.mark.parametrize("n_clients", [2, 5])
@pytest.mark.parametrize("drvs_per_client", [3])
async def test_builder_concurrency(
    n_clients: int,
    drvs_per_client: int,
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Run multiple concurrent clients each building multiple derivations.

    With 1 builder (max_builds=2), we should see serialization of builds
    across clients.
    """
    test_nix = Path(request.config.getoption("--nix"))
    # 1 builder, 2 slots
    stores = make_local_stores(n=1, max_builds=2)

    local_store = LocalSocketStore(
        store_path=Path("/tmp/pynixd-parallel-local"),
        id="local",
        max_builds=0,
        max_transfers=64,
        nix_bin=str(NIX_BIN),
    )

    async with Server(stores=stores, local_store=local_store, ssh_port=0) as server:

        async def _client_task(client_id: int) -> float:
            # Each client uses its own isolated store path to avoid local locking
            client_store = Path(f"/tmp/pynixd-test-parallel-client-{client_id}")
            os.makedirs(client_store, exist_ok=True)
            client_env = nix_env.copy()
            # These env vars are used by test.nix .parallel to sleep/id
            client_env["PYNIXD_PAR_COUNT"] = str(drvs_per_client)
            client_env["PYNIXD_PAR_ID"] = f"c{client_id}"
            # Sleep 1s per drv to make concurrency measurable
            client_env["PYNIXD_PAR_SLEEP"] = "1"

            t0 = time.monotonic()
            rc, stdout, stderr = await (
                nix_command(LIX_BIN)
                .store(str(client_store))
                .builders(
                    server.builder_uri(
                        max_jobs=drvs_per_client, implementation=NixImplementation.LIX
                    )
                )
                .arg("--max-jobs", "0")
                .file(test_nix, "parallel")
                .with_env(client_env)
                .run()
            )
            elapsed = time.monotonic() - t0
            if rc != 0:
                log.error(
                    "client_failed",
                    client_id=client_id,
                    rc=rc,
                    stderr=stderr,
                )
            assert rc == 0, f"Client {client_id} failed:\n{stderr}"
            return elapsed

        start = time.monotonic()
        results = await asyncio.gather(
            *[_client_task(i) for i in range(n_clients)],
            return_exceptions=True,
        )
        total_elapsed = time.monotonic() - start

        failed = [r for r in results if isinstance(r, Exception)]
        assert not failed, f"{len(failed)} clients failed: {failed}"

        client_times = [r for r in results if isinstance(r, float)]

        log.info(
            "concurrency_stats",
            total_wall_clock=f"{total_elapsed:.1f}s",
            client_min=f"{min(client_times):.1f}s",
            client_max=f"{max(client_times):.1f}s",
            client_avg=f"{sum(client_times) / len(client_times):.1f}s",
            sum_client_times=f"{sum(client_times):.1f}s",
            effective_concurrency=f"{sum(client_times) / total_elapsed:.1f}x",
        )

        # With max_builds=2, total wall clock should be at least
        # (n_clients * drvs_per_client * sleep_time) / 2
        expected_min = (n_clients * drvs_per_client * 1.0) / 2
        assert total_elapsed >= expected_min * 0.8, (
            f"Parallelism too high: {total_elapsed:.1f}s < {expected_min:.1f}s"
        )


@pytest.mark.builders
async def test_single_client_max_jobs(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """One client with high --max-jobs should still be limited by builder slots."""
    test_nix = Path(request.config.getoption("--nix"))
    stores = make_local_stores(n=1, max_builds=2)

    local_store = LocalSocketStore(
        store_path=Path("/tmp/pynixd-parallel-local2"),
        id="local",
        max_builds=0,
        max_transfers=64,
        nix_bin=str(NIX_BIN),
    )

    async with Server(stores=stores, local_store=local_store, ssh_port=0) as server:
        # Request 10 jobs from a single client
        rc, _stdout, stderr = await (
            nix_command(LIX_BIN)
            .builders(
                server.builder_uri(max_jobs=10, implementation=NixImplementation.LIX)
            )
            .arg("--max-jobs", "10")
            .file(test_nix, "parallel")
            .with_env(nix_env)
            .run()
        )
        assert rc == 0, f"build failed:\n{stderr}"
