"""Tests for builds targeting a system no store supports."""

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
from pynixd.store import get_current_system


def _wrong_system() -> str:
    """Return a system that is NOT the current one."""
    current = get_current_system()
    return "aarch64-linux" if current != "aarch64-linux" else "x86_64-linux"


@pytest.mark.builders
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
        rc, _stdout, stderr = await (
            nix_command(LIX_BIN)
            .builders(server.builder_uri(implementation=NixImplementation.LIX))
            .file(test_nix, "unsupported")
            .with_env(nix_env)
            .run()
        )
        # It should fail because no builder supports the system
        assert rc != 0
        expected = ["no-substitute", "unsupported system", "not found"]
        assert any(m in stderr for m in expected)


@pytest.mark.store
async def test_unsupported_system_store(
    nix_env: dict[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Build a derivation for an unsupported system via --store."""
    test_nix = Path(request.config.getoption("--nix"))
    stores = make_local_stores(n=1)

    async with Server(stores=stores, ssh_port=0) as server:
        rc, _stdout, stderr = await (
            nix_command(LIX_BIN)
            .store(server.uri(implementation=NixImplementation.LIX))
            .file(test_nix, "unsupported")
            .with_env(nix_env)
            .run()
        )
        assert rc != 0
        expected = ["no-substitute", "unsupported system", "not found"]
        assert any(m in stderr for m in expected)
