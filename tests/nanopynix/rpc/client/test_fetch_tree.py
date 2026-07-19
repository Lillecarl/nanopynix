"""Tests for builtins.fetchTree (gated behind the fetch-tree experimental feature)."""

from __future__ import annotations

import pytest

from nanopynix import NixType
from tests.support.nix_environment import NixTestEnvironment


@pytest.mark.anyio
async def test_fetchtree_with_builtins_prefix(
    isolated_nix_environment: NixTestEnvironment,
) -> None:
    """builtins.fetchTree is registered when the fetch-tree experimental feature is enabled."""
    async with (
        isolated_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        value = await eval.string("builtins.typeOf builtins.fetchTree")
        assert (await value.force_as(NixType.STRING)).rstrip("\n") == "lambda"


@pytest.mark.anyio
async def test_fetchtree_without_builtins_prefix(
    isolated_nix_environment: NixTestEnvironment,
) -> None:
    """fetchTree is also available at top-level scope (without builtins. prefix)."""
    async with (
        isolated_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        value = await eval.string("builtins.typeOf fetchTree")
        assert (await value.force_as(NixType.STRING)).rstrip("\n") == "lambda"
