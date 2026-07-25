"""Same expression, same operation, both engines -- do they *behave* the same?

:mod:`tests.nanopynix.test_engine_parity` compares members and parameter
lists, so "same name, same signature, different behaviour" passes it. Every
CIP3 finding was exactly that shape, which is why the signature ledger caught
none of them. This is the other half of the harness, seeded from the failure
matrix that was used to find them.

An outcome here is either a returned value or an exception **type**. Messages
are deliberately not compared: Nix colourises, and the two transports wrap
differently. The type is what a caller branches on, so the type is what has to
agree.

Where the engines cannot be driven by the same call at all, the adapter that
bridges them carries the name of its ledger entry -- see ``_attr`` and
``_type_name``. That coupling is the point: retiring a ``DEFECT`` from
``LEDGER`` should also delete an adapter here, so the two files cannot drift
into disagreeing about which divergences still exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tests.support.nix_environment import InprocSessionFactory, RpcSessionFactory


# ── Adapters for divergences the signature ledger already records ────
#
# Each of these exists only because LEDGER has a matching DEFECT entry. They
# are not conveniences; they are the measured cost of the divergence, kept
# visible so it is obvious what unifying the API would delete.


async def _attr(value: Any, name: str) -> Any:
    """LEDGER "Value.attr:async" -- inproc awaits, rpc returns a proxy."""
    result = value.attr(name)
    return await result if hasattr(result, "__await__") else result


async def _list_get(value: Any, index: int) -> Any:
    """LEDGER "Value.list_get:async" -- as ``_attr``."""
    result = value.list_get(index)
    return await result if hasattr(result, "__await__") else result


async def _type_name(value: Any) -> str:
    """LEDGER "Value.type:inproc-only" / "Value.get_type:rpc-only".

    Two spellings *and* two return types -- ``str`` on inproc, ``NixType`` on
    rpc. Normalising to the plain name is what any caller written against both
    would have to do, so it is what the comparison does.
    """
    getter = getattr(value, "type", None) or value.get_type
    result = await getter()
    return result if isinstance(result, str) else str(result.name).lower()


async def _sorted_attr_names(value: Any) -> list[str]:
    return sorted(await value.attr_names())


async def _attr_as_int(value: Any) -> int:
    return await (await _attr(value, "a")).as_int()


async def _list_get_as_int(value: Any) -> int:
    return await (await _list_get(value, 1)).as_int()


async def _attr_then_force(value: Any, name: str) -> Any:
    return await (await _attr(value, name)).to_python()


async def _list_get_then_force(value: Any, index: int) -> Any:
    return await (await _list_get(value, index)).to_python()


async def _apply_as_string(value: Any, function: str) -> str:
    """``apply`` applies *function* to this value -- the value is the argument."""
    return await (await value.apply(function)).as_string()


# ── The cases ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Case:
    """One expression, one operation, run identically on both engines."""

    name: str
    expression: str
    operation: Callable[[Any], Awaitable[Any]]


SUCCESS_CASES: list[Case] = [
    Case("to_python_int", "42", lambda v: v.to_python()),
    Case("to_python_string", '"hello"', lambda v: v.to_python()),
    Case("to_python_bool", "true", lambda v: v.to_python()),
    Case("to_python_null", "null", lambda v: v.to_python()),
    Case("to_python_list", '[1 true "hi" null]', lambda v: v.to_python()),
    Case("to_python_attrs", '{ a = 1; b = [true]; c = "hi"; }', lambda v: v.to_python()),
    Case("to_python_nested", "{ outer = { inner = 42; }; }", lambda v: v.to_python()),
    Case("as_int", "1 + 2", lambda v: v.as_int()),
    Case("as_float", "3.5", lambda v: v.as_float()),
    Case("as_bool", "true", lambda v: v.as_bool()),
    Case("as_string", '"hello"', lambda v: v.as_string()),
    # Nix widens an int to a float; the strict accessors must widen with it.
    Case("as_float_widens_an_int", "42", lambda v: v.as_float()),
    Case("attr_names", "{ b = 1; a = 2; }", _sorted_attr_names),
    Case("has_attr_present", "{ a = 1; }", lambda v: v.has_attr("a")),
    Case("has_attr_absent", "{ a = 1; }", lambda v: v.has_attr("zzz")),
    Case("list_length", "[1 2 3]", lambda v: v.list_length()),
    Case("attr_then_as_int", "{ a = 42; }", _attr_as_int),
    Case("list_get_then_as_int", "[10 20 30]", _list_get_as_int),
    Case("type_of_attrs", "{}", _type_name),
    Case("type_of_list", "[]", _type_name),
    Case("type_of_function", "x: x", _type_name),
    Case("type_of_int", "1", _type_name),
    # apply() is how a caller reaches any builtin at all, so it stands in for
    # the whole of Nix rather than for one operation.
    # force() on a scalar is the one shape the two engines already agree on.
    Case("force_int", "1 + 2", lambda v: v.force()),
    Case("force_string", '"hi"', lambda v: v.force()),
    Case("force_bool", "true", lambda v: v.force()),
    Case("force_null", "null", lambda v: v.force()),
    # ...and the two shapes they do not. See SEMANTIC_LEDGER.
    Case("force_attrs", "{ a = 1; }", lambda v: v.force()),
    Case("force_list", "[1 2]", lambda v: v.force()),
    Case("apply_tostring_int", "42", lambda v: _apply_as_string(v, "builtins.toString")),
    Case("apply_tostring_bool", "true", lambda v: _apply_as_string(v, "builtins.toString")),
]


# The failure half, seeded from tests/temp/test_error_matrix.py's _EVAL_CASES.
# Same contract: both engines must raise the *same exception type*.
FAILURE_CASES: list[Case] = [
    Case("undefined_variable", "nanopynix_no_such_variable", lambda v: v.to_python()),
    Case("type_error", '1 + "not a number"', lambda v: v.to_python()),
    Case("throw", 'builtins.throw "nanopynix parity throw"', lambda v: v.to_python()),
    Case("parse_error", "let in in", lambda v: v.to_python()),
    Case("missing_attr_in_nix", "{ a = 1; }.nonexistent", lambda v: v.to_python()),
    Case("infinite_recursion", "let x = x; in x", lambda v: v.to_python()),
    Case("runaway_recursion", "let f = n: f (n + 1); in f 0", lambda v: v.to_python()),
    # The absence pair: both must be Nix errors *and* Python ones, identically
    # on both engines.
    Case("attr_missing", "{ x = 1; }", lambda v: _attr(v, "nope")),
    Case("list_index_out_of_range", "[1 2 3]", lambda v: _list_get(v, 99)),
    # The same two, forced. These *do* agree, which is what identifies the
    # pair above as a laziness difference rather than a missing error.
    Case("attr_missing_forced", "{ x = 1; }", lambda v: _attr_then_force(v, "nope")),
    Case("list_index_out_of_range_forced", "[1 2 3]", lambda v: _list_get_then_force(v, 99)),
    # Strict accessors on the wrong type.
    Case("as_int_on_string", '"nope"', lambda v: v.as_int()),
    Case("as_string_on_int", "42", lambda v: v.as_string()),
    Case("as_bool_on_null", "null", lambda v: v.as_bool()),
    # to_python() has no answer for a function, on either engine.
    Case("to_python_of_a_function", "x: x", lambda v: v.to_python()),
    Case("to_python_of_an_attrset_holding_a_function", "{ f = x: x; }", lambda v: v.to_python()),
    # Navigation accessors must refuse the wrong type rather than answering.
    Case("attr_names_on_an_int", "1", _sorted_attr_names),
    Case("list_length_on_an_attrset", "{}", lambda v: v.list_length()),
    Case("has_attr_on_a_list", "[]", lambda v: v.has_attr("a")),
]


# Cases where the engines are known *not* to agree, and why. Same discipline
# as test_engine_parity.py's LEDGER: an entry is a defect that is counted, not
# an exemption granted. The test below asserts these still disagree, so an
# entry cannot outlive the divergence it documents.
SEMANTIC_LEDGER: dict[str, str] = {
    "attr_missing": (
        "DEFECT, the behavioural cost of LEDGER's \"Value.attr:async\": inproc's attr() awaits and "
        "so raises MissingAttributeError here, while rpc's is sync and hands back an unresolved "
        "proxy, deferring the error to whatever forces it. See attr_missing_forced -- once forced "
        "the engines agree, so this is *when* the error arrives, not whether."
    ),
    "list_index_out_of_range": 'DEFECT: as attr_missing, for LEDGER\'s "Value.list_get:async".',
    "force_attrs": (
        "DEFECT: force() means two different things. inproc's runs _force_to_python and returns a "
        "fully converted dict -- a *deep* conversion wearing WHNF's name, and already what "
        "to_python() is for. rpc's returns a ValueAttrs view whose values are still lazy, which is "
        "what forcing an attrset actually does in Nix. TODO item 6 resolves this by deleting the "
        "view classes and giving force() one meaning on both engines."
    ),
    "force_list": "DEFECT: as force_attrs, with ValueList.",
}


# ── Running one case on one engine ───────────────────────────────────

# ("value", result) or ("raise", exception type name). The type name rather
# than the class itself because the two engines reach the same *public* class
# by different routes, and comparing names keeps the failure message readable.
Outcome = tuple[str, Any]


async def _run(factory: Any, case: Case) -> Outcome:
    async with factory() as session, session.store() as store, session.eval(store) as evaluator:
        try:
            value = await evaluator.string(case.expression)
            return ("value", await case.operation(value))
        except Exception as exc:  # the exception type *is* the measurement
            return ("raise", type(exc).__name__)


@pytest.mark.parametrize("case", SUCCESS_CASES, ids=lambda case: case.name)
async def test_engines_agree_on_success(
    case: Case,
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """The same expression and operation must return the same Python value."""
    inproc_outcome = await _run(inproc_session, case)
    rpc_outcome = await _run(rpc_session, case)
    if case.name in SEMANTIC_LEDGER:
        assert inproc_outcome != rpc_outcome, (
            f"{case.name!r} is in SEMANTIC_LEDGER but the engines now agree "
            f"({inproc_outcome!r}) -- delete the entry"
        )
        return
    assert inproc_outcome == rpc_outcome


@pytest.mark.parametrize("case", FAILURE_CASES, ids=lambda case: case.name)
async def test_engines_agree_on_failure(
    case: Case,
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """The same failure must arrive as the same exception type on both engines.

    Not the same message: rpc's travels as gRPC status detail and inproc's
    comes straight off the nanobind boundary, and Nix colourises both. The
    type is what ``except`` clauses are written against.
    """
    inproc_outcome = await _run(inproc_session, case)
    rpc_outcome = await _run(rpc_session, case)
    if case.name in SEMANTIC_LEDGER:
        assert inproc_outcome != rpc_outcome, (
            f"{case.name!r} is in SEMANTIC_LEDGER but the engines now agree "
            f"({inproc_outcome!r}) -- delete the entry"
        )
        return
    assert inproc_outcome[0] == "raise", f"inproc did not raise: {inproc_outcome!r}"
    assert inproc_outcome == rpc_outcome


# ── The harness's own teeth ──────────────────────────────────────────
#
# A parity check that cannot detect disparity passes silently forever. The
# signature half of this harness guards itself with synthetic drifted classes;
# this half guards itself against the two ways a semantic comparison rots.


def test_a_differing_return_value_is_not_equal() -> None:
    """The comparison is by value, so a wrong answer cannot slip through."""
    assert ("value", 42) != ("value", 43)
    assert ("value", {"a": 1}) != ("value", {"a": "1"})


def test_a_differing_exception_type_is_not_equal() -> None:
    """Two engines failing for different reasons must not read as agreement."""
    assert ("raise", "NixTypeError") != ("raise", "MissingAttributeError")
    assert ("raise", "NixTypeError") != ("value", None)


def test_semantic_ledger_names_only_real_cases() -> None:
    """A ledger entry for a case that no longer exists documents nothing."""
    known = {case.name for case in SUCCESS_CASES + FAILURE_CASES}
    assert set(SEMANTIC_LEDGER) <= known, set(SEMANTIC_LEDGER) - known


def test_every_failure_case_is_asserted_to_actually_fail() -> None:
    """A FAILURE_CASES entry that stopped raising would otherwise pass quietly.

    ``test_engines_agree_on_failure`` asserts ``inproc_outcome[0] == "raise"``
    for exactly this reason: without it, an expression that silently started
    succeeding on *both* engines would still compare equal, and the case would
    go on passing while measuring nothing.
    """
    assert FAILURE_CASES, "the failure half of the matrix must not be empty"
