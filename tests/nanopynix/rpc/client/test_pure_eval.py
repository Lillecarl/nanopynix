"""Tests for pure/impure evaluation control via ``session.eval(store, eval_settings=...)``."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# nanopynix types (Session, NixType, current_system, etc.) are C++ nanobind
# extensions without type stubs; all member/variable types are Unknown.

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nanopynix import NixEvalSettings, NixType, current_system
from nanopynix.exceptions import EvalError

if TYPE_CHECKING:
    from tests.support.nix_environment import NixTestEnvironment


@pytest.mark.anyio
async def test_pure_eval_blocks_impure_builtins(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """pure_eval=True removes impure constants from the base environment."""
    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store, eval_settings=NixEvalSettings(pure_eval=True)) as eval,
    ):
        # currentTime and currentSystem are not added when pureEval is on.
        with pytest.raises(EvalError, match="currentTime"):
            await eval.string("builtins.currentTime")

        with pytest.raises(EvalError, match="currentSystem"):
            await eval.string("builtins.currentSystem")


@pytest.mark.anyio
async def test_impure_allows_impure_builtins(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """pure_eval=False (default) allows currentTime and currentSystem."""
    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store, eval_settings=NixEvalSettings(pure_eval=False)) as eval,
    ):
        v = await eval.string("builtins.currentTime")
        assert await v.force_as(NixType.INT) > 0

        system = await eval.string("builtins.currentSystem")
        assert isinstance(await system.force(), str)


async def test_current_system_binding_matches_builtin_default(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store, eval_settings=NixEvalSettings(pure_eval=False)) as eval,
    ):
        system = await eval.string("builtins.currentSystem")
        assert await system.force() == current_system()


@pytest.mark.anyio
async def test_default_is_impure(shared_nix_environment: NixTestEnvironment) -> None:
    """Omitting eval_settings defaults to impure (pure_eval=False)."""
    async with shared_nix_environment.rpc_session() as session, session.store() as store, session.eval(store) as eval:
        v = await eval.string("builtins.currentTime")
        assert await v.force_as(NixType.INT) > 0


@pytest.mark.anyio
async def test_restrict_eval_blocks_absolute_paths(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """restrict_eval=True blocks readFile of paths outside the Nix store."""
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        eval_settings = NixEvalSettings(pure_eval=True, restrict_eval=True)
        async with session.eval(store, eval_settings=eval_settings) as eval:
            # Any absolute path outside the store is forbidden.
            with pytest.raises(EvalError, match="is forbidden"):
                await eval.string('builtins.readFile "/etc/hostname"')


@pytest.mark.anyio
async def test_restrict_eval_allows_pure_attrs(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """restrict_eval=True still allows ordinary pure evaluation."""
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        eval_settings = NixEvalSettings(pure_eval=True, restrict_eval=True)
        async with session.eval(store, eval_settings=eval_settings) as eval:
            v = await eval.string('{ name = "pure-test"; }')
            assert await v.get_type() == NixType.ATTRS
            assert await v.has_attr("name") is True


@pytest.mark.anyio
async def test_allowed_uris_is_exposed(shared_nix_environment: NixTestEnvironment) -> None:
    """allowed_uris param reaches the worker without error."""
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        eval_settings = NixEvalSettings(pure_eval=True, allowed_uris=["https://github.com"])
        async with session.eval(store, eval_settings=eval_settings) as eval:
            # Just smoke-test: eval should work with allowed_uris set.
            v = await eval.string("1 + 1")
            assert await v.force_as(NixType.INT) == 2


@pytest.mark.anyio
@pytest.mark.concurrency
async def test_concurrent_eval_sessions_have_independent_pure_eval(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """Two concurrently open EvalSessions may have different pure_eval values.

    Regression test: eval settings used to be applied through a process-wide
    (L2) / worker-wide (L3) mutation, so two concurrently open evaluators
    could not disagree on pure_eval. Each EvalSession now owns its own
    EvalState with its own settings.
    """
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        pure = session.eval(store, eval_settings=NixEvalSettings(pure_eval=True))
        impure = session.eval(store, eval_settings=NixEvalSettings(pure_eval=False))
        await pure.open()
        await impure.open()

        with pytest.raises(EvalError, match="currentTime"):
            await pure.string("builtins.currentTime")

        v = await impure.string("builtins.currentTime")
        assert await v.force_as(NixType.INT) > 0

        await pure.close()
        await impure.close()
