"""Integration tests for manager-side RPC primops."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# Session / nanopynix are C++ nanobind extensions without type stubs.

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nanopynix.models import PrimOpSpec

if TYPE_CHECKING:
    from tests.support.nix_environment import NixTestEnvironment

pytestmark = pytest.mark.nix_version(minimum="2.32")


def _rpc_double(x: int) -> int:
    return x * 2


def _rpc_greet(name: str) -> str:
    return f"Hello from manager, {name}!"


def _rpc_add(a: int, b: int) -> int:
    return a + b


async def test_manager_rpc_primop_double(shared_nix_environment: NixTestEnvironment) -> None:
    spec = PrimOpSpec(
        name="managerDouble",
        arity=1,
        args=["x"],
        doc="RPC primop: double an int",
        rpc=True,
    )
    async with (
        shared_nix_environment.rpc_session(primops=[spec], primop_callables={"managerDouble": _rpc_double}) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        result = await eval.string("builtins.managerDouble 21")
        assert await result.force() == 42


async def test_manager_rpc_primop_greet(shared_nix_environment: NixTestEnvironment) -> None:
    spec = PrimOpSpec(
        name="managerGreet",
        arity=1,
        args=["name"],
        doc="RPC primop: greet a name",
        rpc=True,
    )
    async with (
        shared_nix_environment.rpc_session(primops=[spec], primop_callables={"managerGreet": _rpc_greet}) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        result = await eval.string('builtins.managerGreet "World"')
        assert await result.force() == "Hello from manager, World!"


async def test_manager_rpc_primop_add(shared_nix_environment: NixTestEnvironment) -> None:
    spec = PrimOpSpec(
        name="managerAdd",
        arity=2,
        args=["a", "b"],
        doc="RPC primop: add two ints",
        rpc=True,
    )
    async with (
        shared_nix_environment.rpc_session(primops=[spec], primop_callables={"managerAdd": _rpc_add}) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        result = await eval.string("builtins.managerAdd 3 7")
        assert await result.force() == 10


async def test_manager_rpc_primop_lambda(shared_nix_environment: NixTestEnvironment) -> None:
    """Inlined lambdas work — no import path needed."""
    spec = PrimOpSpec(
        name="managerTriple",
        arity=1,
        args=["x"],
        doc="RPC primop: triple an int",
        rpc=True,
    )
    async with (
        shared_nix_environment.rpc_session(primops=[spec], primop_callables={"managerTriple": lambda x: x * 3}) as session,  # type: ignore[reportUnknownLambdaType] -- lambda within dict value has no type context
        session.store() as store,
        session.eval(store) as eval,
    ):
        result = await eval.string("builtins.managerTriple 7")
        assert await result.force() == 21
