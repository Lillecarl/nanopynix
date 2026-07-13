"""Tests for pure/impure evaluation control via Session(pure_eval=...)."""

from __future__ import annotations

import pytest

from nanopynix import NixType, Session, current_system
from nanopynix.exceptions import EvalError


@pytest.mark.anyio
async def test_pure_eval_blocks_impure_builtins():
    """pure_eval=True removes impure constants from the base environment."""
    async with (
        Session(pure_eval=True) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        # currentTime and currentSystem are not added when pureEval is on.
        with pytest.raises(EvalError, match="currentTime"):
            await eval.string("builtins.currentTime")

        with pytest.raises(EvalError, match="currentSystem"):
            await eval.string("builtins.currentSystem")


@pytest.mark.anyio
async def test_impure_allows_impure_builtins():
    """pure_eval=False (default) allows currentTime and currentSystem."""
    async with (
        Session(pure_eval=False) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string("builtins.currentTime")
        assert await v.force_as(NixType.INT) > 0

        system = await eval.string("builtins.currentSystem")
        assert isinstance(await system.force(), str)


async def test_current_system_binding_matches_builtin_default():
    async with (
        Session(pure_eval=False) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        system = await eval.string("builtins.currentSystem")
        assert await system.force() == current_system()


@pytest.mark.anyio
async def test_default_is_impure():
    """Omitting pure_eval defaults to impure (pure_eval=False)."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string("builtins.currentTime")
        assert await v.force_as(NixType.INT) > 0


@pytest.mark.anyio
async def test_restrict_eval_blocks_absolute_paths():
    """restrict_eval=True blocks readFile of paths outside the Nix store."""
    async with (
        Session(pure_eval=True, restrict_eval=True) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        # Any absolute path outside the store is forbidden.
        with pytest.raises(EvalError, match="is forbidden"):
            await eval.string('builtins.readFile "/etc/hostname"')


@pytest.mark.anyio
async def test_restrict_eval_allows_pure_attrs():
    """restrict_eval=True still allows ordinary pure evaluation."""
    async with (
        Session(pure_eval=True, restrict_eval=True) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string('{ name = "pure-test"; }')
        assert await v.get_type() == NixType.ATTRS
        assert await v.has_attr("name") is True


@pytest.mark.anyio
async def test_allowed_uris_is_exposed():
    """allowed_uris param reaches the worker without error."""
    async with (
        Session(pure_eval=True, allowed_uris=["https://github.com"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        # Just smoke-test: eval should work with allowed_uris set.
        v = await eval.string("1 + 1")
        assert await v.force_as(NixType.INT) == 2
