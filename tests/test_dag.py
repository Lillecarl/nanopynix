"""DAG-aware build scheduler tests.

Verifies that pynixd correctly schedules builds based on input availability,
handling dependencies that must be built or transferred before their
dependents can start.
"""

from __future__ import annotations

import pytest
from conftest import (
    make_local_stores,
    nix_build,
    nix_build_store_only,
    run_pynixd,
)


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_builders(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .dag (3-layer dependency DAG) via --builders."""
    test_nix = request.config.getoption("--nix")
    stores = make_local_stores(n=2)

    async with run_pynixd(stores) as server:
        rc, _stdout, stderr = await nix_build(
            server.builder_uri(),
            "dag",
            nix_env,
            nix_file=test_nix,
        )
        assert rc == 0, f"DAG build failed:\n{stderr}"


@pytest.mark.store
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_store(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .dag (3-layer dependency DAG) via --store."""
    test_nix = request.config.getoption("--nix")
    stores = make_local_stores(n=2)

    async with run_pynixd(stores) as server:
        rc, _stdout, stderr = await nix_build_store_only(
            server.uri,
            "dag",
            nix_env,
            nix_file=test_nix,
        )
        assert rc == 0, f"DAG build failed:\n{stderr}"
