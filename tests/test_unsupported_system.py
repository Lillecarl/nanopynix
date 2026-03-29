"""Tests for builds targeting a system no store supports."""

from __future__ import annotations

import pytest

from conftest import (
    get_current_system,
    make_local_stores,
    nix_build,
    nix_build_store_only,
    run_pynixd,
)


def _wrong_system() -> str:
    """Return a system that is NOT the current one."""
    current = get_current_system()
    return "aarch64-linux" if current != "aarch64-linux" else "x86_64-linux"


@pytest.mark.builders
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_builders(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build fails via --builders when no store supports the system."""
    test_nix = request.config.getoption("--nix")
    wrong = _wrong_system()
    stores = make_local_stores(n=1, supported_systems=[wrong])

    async with run_pynixd(stores) as server:
        returncode, stdout, stderr = await nix_build(
            server.builder_uri(), server.client_store_path, nix_env,
            "--file", test_nix, "simple",
            timeout=30,
        )
        assert returncode != 0, "Build should have failed"
        assert wrong in stderr, (
            f"Expected stderr to mention available system {wrong!r}.\n"
            f"stderr: {stderr}"
        )


@pytest.mark.store
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_store(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build fails via --store when no store supports the system."""
    test_nix = request.config.getoption("--nix")
    wrong = _wrong_system()
    stores = make_local_stores(n=1, supported_systems=[wrong])

    async with run_pynixd(stores) as server:
        returncode, stdout, stderr = await nix_build_store_only(
            server.uri, nix_env,
            "--file", test_nix, "simple",
            timeout=30,
        )
        assert returncode != 0, "Build should have failed"
        assert wrong in stderr, (
            f"Expected stderr to mention available system {wrong!r}.\n"
            f"stderr: {stderr}"
        )
