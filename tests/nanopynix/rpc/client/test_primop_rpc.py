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
        assert await result.as_int() == 42


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
        assert await result.as_string() == "Hello from manager, World!"


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
        assert await result.as_int() == 10


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
        shared_nix_environment.rpc_session(
            primops=[spec],
            primop_callables={"managerTriple": lambda x: x * 3},  # type: ignore[reportUnknownLambdaType] -- lambda within dict value has no type context
        ) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        result = await eval.string("builtins.managerTriple 7")
        assert await result.as_int() == 21


def _rpc_reshape(request: dict[str, object]) -> dict[str, object]:
    """Round-trips a nested attrset/list arg into a nested attrset/list
    result, with no ``toJSON``/``fromJSON`` on either side of the RPC call --
    exercises ``DeepValue`` carrying real structure over the wire instead of
    the old JSON-string-scalar workaround."""
    names = [str(item["name"]) for item in request["items"]]  # type: ignore[index] -- test-only, shape is known
    return {
        "count": len(names),
        "names": names,
        "nested": {"first": names[0], "tags": request["tags"]},
    }


async def test_manager_rpc_primop_deep_value(shared_nix_environment: NixTestEnvironment) -> None:
    """A single arg carrying nested attrsets/lists, and a nested result, both
    cross the RPC primop backchannel as real ``DeepValue`` structure."""
    spec = PrimOpSpec(
        name="managerReshape",
        arity=1,
        args=["request"],
        doc="RPC primop: reshape a nested attrset/list argument",
        rpc=True,
    )
    async with (
        shared_nix_environment.rpc_session(
            primops=[spec], primop_callables={"managerReshape": _rpc_reshape}
        ) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        result = await eval.string("""
            builtins.managerReshape {
              items = [ { name = "a"; } { name = "b"; } ];
              tags = [ "x" "y" ];
            }
        """)
        assert await result.to_python() == {
            "count": 2,
            "names": ["a", "b"],
            "nested": {"first": "a", "tags": ["x", "y"]},
        }
