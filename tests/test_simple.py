"""Simple single-derivation build tests."""

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
@pytest.mark.timeout(60)
async def test_builders(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --builders."""
    test_nix = request.config.getoption("--nix")
    stores = make_local_stores(n=2)

    async with run_pynixd(stores) as server:
        returncode, stdout, stderr = await nix_build(
            server.builder_uri(), server.client_store_path, nix_env,
            "--file", test_nix, "simple",
        )
        assert returncode == 0, f"nix build failed:\n{stderr}"


@pytest.mark.store
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_store(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --store."""
    test_nix = request.config.getoption("--nix")
    stores = make_local_stores(n=2)

    async with run_pynixd(stores) as server:
        returncode, stdout, stderr = await nix_build_store_only(
            server.uri, nix_env,
            "--file", test_nix, "simple",
        )
        assert returncode == 0, f"nix build --store failed:\n{stderr}"
