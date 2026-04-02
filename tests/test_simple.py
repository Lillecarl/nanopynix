"""Simple end-to-end build tests."""

from __future__ import annotations

from pathlib import Path

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
    """Build test.nix .simple via --builders."""
    test_nix = Path(request.config.getoption("--nix"))
    stores = make_local_stores(n=2)

    async with run_pynixd(stores) as server:
        rc, _stdout, stderr = await nix_build(
            server.builder_uri(),
            "simple",
            nix_env,
            nix_file=test_nix,
        )
        assert rc == 0, f"simple build failed:\n{stderr}"


@pytest.mark.store
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_store(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --store."""
    test_nix = Path(request.config.getoption("--nix"))
    stores = make_local_stores(n=2)

    async with run_pynixd(stores) as server:
        rc, _stdout, stderr = await nix_build_store_only(
            server.uri,
            "simple",
            nix_env,
            nix_file=test_nix,
        )
        assert rc == 0, f"simple build failed:\n{stderr}"
