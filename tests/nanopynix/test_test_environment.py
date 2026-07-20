"""Regression tests for the hermetic pytest Nix environments."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.support.nix_environment import NixTestEnvironment


@pytest.mark.anyio
async def test_isolated_environment_uses_its_configured_store(
    isolated_nix_environment: NixTestEnvironment,
) -> None:
    environment = isolated_nix_environment
    async with environment.rpc_session() as nix, nix.store() as store:
        uri = await store.uri(with_params=True)
    if environment.backend == "local":
        assert uri.startswith("local")
    else:
        assert environment.store_uri_matches(uri)


@pytest.mark.anyio
async def test_local_and_daemon_roots_are_backend_specific(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    environment = shared_nix_environment
    assert environment.root.name.startswith(f"nix-{environment.backend}-shared")
    assert environment.store_uri != "auto"


@pytest.mark.anyio
async def test_inproc_session_uses_its_configured_store(
    isolated_nix_environment: NixTestEnvironment,
) -> None:
    environment = isolated_nix_environment
    async with environment.inproc_session() as nix, nix.store() as store:
        uri = await store.uri(with_params=True)
    if environment.backend == "local":
        assert uri.startswith("local")
    else:
        assert uri == environment.store_uri
