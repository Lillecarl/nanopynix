"""Parallelism pressure tests.

Builds independent 2-second derivations and compares wall-clock time
to verify that builds are distributed across stores.

test.nix .parallel reads PYNIXD_PAR_COUNT (default 100) to decide
how many leaves to generate. Each leaf contains builtins.currentTime
so every evaluation produces fresh derivations.

Tests:
- test_multi_client: 10 concurrent nix clients each building 10 derivations
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import pytest

from conftest import (
    _run_subprocess_with_timeout,
    make_local_stores,
    nix_build,
    nix_build_store_only,
    run_pynixd,
    NIX_BIN,
)

log = logging.getLogger(__name__)

pytestmark = pytest.mark.parallel


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_multi_client(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """10 concurrent nix clients each building 10 derivations against pynixd."""
    test_nix = request.config.getoption("--nix")
    n_clients = 10
    drvs_per_client = 10

    stores = make_local_stores(n=4, prefix="mc")

    async with run_pynixd(
        stores, njobs=100,
        client_store_path="/tmp/pynixd-test-multiclient",
    ) as server:

        async def _run_client(client_id: int) -> tuple[int, float]:
            client_store = f"/tmp/pynixd-test-mc-client-{client_id}"
            os.makedirs(client_store, exist_ok=True)
            client_env = nix_env.copy()
            client_env["PYNIXD_PAR_COUNT"] = str(drvs_per_client)
            client_env["PYNIXD_PAR_ID"] = f"c{client_id}"
            cmd = [
                NIX_BIN, "build",
                "--store", client_store,
                "--builders", server.builder_uri(),
                "--max-jobs", "0",
                "--no-link",
                "--file", test_nix, "parallel",
            ]
            t0 = time.monotonic()
            rc, stdout, stderr = await _run_subprocess_with_timeout(
                cmd, client_env, timeout=300,
            )
            elapsed = time.monotonic() - t0
            if rc != 0:
                log.error("Client %d failed: %s", client_id, stderr[:500])
            return rc, elapsed

        start = time.monotonic()
        results = await asyncio.gather(
            *[_run_client(i) for i in range(n_clients)]
        )
        total_elapsed = time.monotonic() - start

        failed = [(i, rc) for i, (rc, _) in enumerate(results) if rc != 0]
        client_times = [elapsed for _, elapsed in results]

        print(
            f"\n  Total wall-clock: {total_elapsed:.1f}s"
            f"\n  Client times: min={min(client_times):.1f}s "
            f"max={max(client_times):.1f}s avg={sum(client_times)/len(client_times):.1f}s"
            f"\n  Sum of client times: {sum(client_times):.1f}s"
            f"\n  Effective concurrency: {sum(client_times)/total_elapsed:.1f}x"
        )

        assert not failed, f"{len(failed)} clients failed: {failed}"
        # 100 total builds × 2s each. With 4 stores × 2 slots = 8 concurrent.
        # Ideal ~25s. Allow generous margin.
        assert total_elapsed < 180, (
            f"Multi-client build took {total_elapsed:.1f}s, expected < 180s"
        )


@pytest.mark.store
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_store_parallel(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Single client builds 100 derivations via --store.

    pynixd decomposes the BuildPaths into individual BuildDerivation
    requests and distributes them across stores internally.
    """
    test_nix = request.config.getoption("--nix")

    stores = make_local_stores(n=4, prefix="sp")

    async with run_pynixd(
        stores, njobs=100,
        client_store_path="/tmp/pynixd-test-store-parallel",
    ) as server:
        client_env = nix_env.copy()
        client_env["PYNIXD_PAR_COUNT"] = "100"

        start = time.monotonic()
        rc, stdout, stderr = await nix_build_store_only(
            server.uri, client_env,
            "--file", test_nix, "parallel",
            timeout=300,
        )
        elapsed = time.monotonic() - start

        print(f"\n  Wall-clock: {elapsed:.1f}s (100 × 2s drvs, 4 stores × 2 slots)")

        assert rc == 0, f"Store parallel build failed:\n{stderr}"
        # 100 builds × 2s, 8 concurrent slots → ideal ~25s. Allow margin.
        assert elapsed < 180, (
            f"Store parallel build took {elapsed:.1f}s, expected < 180s"
        )
