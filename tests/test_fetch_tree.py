"""Tests for builtins.fetchTree (gated behind the fetch-tree experimental feature)."""

from __future__ import annotations

import pytest

from nanopynix import NixType, Session


@pytest.mark.anyio
async def test_fetchTree_with_builtins_prefix():
    """builtins.fetchTree is registered when the fetch-tree experimental feature is enabled."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        value = await eval.string("builtins.typeOf builtins.fetchTree")
        assert (await value.force_as(NixType.STRING)).rstrip("\n") == "lambda"


@pytest.mark.anyio
async def test_fetchTree_without_builtins_prefix():
    """fetchTree is also available at top-level scope (without builtins. prefix)."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        value = await eval.string("builtins.typeOf fetchTree")
        assert (await value.force_as(NixType.STRING)).rstrip("\n") == "lambda"
