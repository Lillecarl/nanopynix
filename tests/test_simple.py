"""Simple end-to-end build tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    LIX_BIN,
    make_local_stores,
    nix_command,
)

from pynixd import Server
from pynixd.instance import NixImplementation


@pytest.mark.builders
@pytest.mark.timeout(180)
async def test_builders(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --builders."""
    test_nix = Path(request.config.getoption("--nix"))
    stores = make_local_stores(n=2)

    async with Server(stores=stores, ssh_port=0) as server:
        rc, _stdout, stderr = await (
            nix_command(LIX_BIN)
            .builders(server.builder_uri(implementation=NixImplementation.LIX))
            .file(test_nix, "simple")
            .with_env(nix_env)
            .run()
        )
        assert rc == 0, f"simple build failed:\n{stderr}"


@pytest.mark.store
@pytest.mark.timeout(180)
async def test_store(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build test.nix .simple via --store."""
    test_nix = Path(request.config.getoption("--nix"))
    stores = make_local_stores(n=2)

    async with Server(stores=stores, ssh_port=0) as server:
        rc, _stdout, stderr = await (
            nix_command(LIX_BIN)
            .store(server.uri(implementation=NixImplementation.LIX))
            .file(test_nix, "simple")
            .with_env(nix_env)
            .run()
        )
        assert rc == 0, f"simple build failed:\n{stderr}"
