"""Tests for builds targeting a system no store supports."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    make_local_stores,
    nix_build,
    nix_build_store_only,
)

from pynixd import Server
from pynixd.store import get_current_system


def _wrong_system() -> str:
    """Return a system that is NOT the current one."""
    current = get_current_system()
    return "aarch64-linux" if current != "aarch64-linux" else "x86_64-linux"


@pytest.mark.builders
@pytest.mark.asyncio
async def test_unsupported_system_builders(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build a derivation for an unsupported system via --builders."""
    test_nix = Path(request.config.getoption("--nix"))
    stores = make_local_stores(n=1)

    # All stores in pynixd report only the current system by default.
    # If we request a build for a different system, it should fail.
    async with Server(stores=stores, ssh_port=0) as server:
        rc, _stdout, stderr = await nix_build(
            server.builder_uri(),
            "unsupported",
            nix_env,
            nix_file=test_nix,
        )
        # It should fail because no builder supports the system
        assert rc != 0
        assert "no-substitute" in stderr or "unsupported system" in stderr


@pytest.mark.store
@pytest.mark.asyncio
async def test_unsupported_system_store(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build a derivation for an unsupported system via --store."""
    test_nix = Path(request.config.getoption("--nix"))
    stores = make_local_stores(n=1)

    async with Server(stores=stores, ssh_port=0) as server:
        rc, _stdout, stderr = await nix_build_store_only(
            server.uri(),
            "unsupported",
            nix_env,
            nix_file=test_nix,
        )
        assert rc != 0
        assert "no-substitute" in stderr or "unsupported system" in stderr
