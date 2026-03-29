"""Multi-layer DAG build tests."""

from __future__ import annotations

import pytest

from conftest import (
    make_local_stores,
    nix_build,
    nix_build_store_only,
    run_pynixd,
)

pytestmark = pytest.mark.dag


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
        returncode, stdout, stderr = await nix_build(
            server.builder_uri(), server.client_store_path, nix_env,
            "--file", test_nix, "dag",
        )
        assert returncode == 0, f"DAG build failed:\n{stderr}"


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
        returncode, stdout, stderr = await nix_build_store_only(
            server.uri, nix_env,
            "--file", test_nix, "dag",
        )
        assert returncode == 0, f"DAG build failed:\n{stderr}"
