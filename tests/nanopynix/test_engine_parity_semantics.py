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
bridges them carries the name of its ledger entry -- see ``_type_name``. That
coupling is the point: retiring a ``DEFECT`` from ``LEDGER`` should also
delete an adapter here, so the two files cannot drift into disagreeing about
which divergences still exist. ``_attr`` and ``_list_get`` are gone for
exactly that reason: selection is now lazy and synchronous on both engines,
so the cases below call ``value.attr(...)`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from tests.support.nix_environment import InprocSessionFactory, RpcSessionFactory


# ── Adapters for divergences the signature ledger already records ────
#
# Each of these exists only because LEDGER has a matching DEFECT entry. They
# are not conveniences; they are the measured cost of the divergence, kept
# visible so it is obvious what unifying the API would delete.


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
    return await value.attr("a").as_int()


async def _list_get_as_int(value: Any) -> int:
    return await value.list_get(1).as_int()


async def _as_dict_keys(value: Any) -> list[str]:
    return sorted(await value.as_dict())


async def _as_dict_values(value: Any) -> dict[str, Any]:
    return {name: await child.to_python() for name, child in sorted((await value.as_dict()).items())}


async def _as_list_values(value: Any) -> list[Any]:
    return [await child.to_python() for child in await value.as_list()]


async def _apply_via_as_dict(value: Any) -> Any:
    """A function leaf reached through as_dict() is still callable.

    ``apply`` applies its argument *to* the receiver, so this is ``f n`` --
    both of them handles handed back by the same ``as_dict()`` call.
    """
    entries = await value.as_dict()
    return await (await entries["n"].apply(entries["f"])).to_python()


async def _selection_defers(select: Any, selector: Any) -> str:
    """Selecting something absent must not raise until something forces it.

    Both engines defer now, so both reach the marker; one that resolved
    eagerly would raise instead and the comparison below would catch it. The
    forced half of this pair is in FAILURE_CASES, where both raise.

    The second selection is what makes this sensitive to *how* an engine
    defers rather than only to whether it raises: chaining off the first
    result is possible only if that result is a value. An engine whose
    selection returned a coroutine would fail here with ``AttributeError``
    instead of reaching the marker.
    """
    child = select(selector)
    child.attr("also-absent")
    return "deferred"


async def _attr_then_force(value: Any, name: str) -> Any:
    return await value.attr(name).to_python()


async def _list_get_then_force(value: Any, index: int) -> Any:
    return await value.list_get(index).to_python()


async def _curried_call(value: Any, *args: Any) -> Any:
    """Call a Nix function with several arguments in one ``call()``.

    Retires LEDGER's "Value.call:params". inproc took exactly one ``argument``
    until both engines moved onto ``CoreValue.call(*arguments)``, so this case
    raised ``TypeError`` on one engine and answered on the other.
    """
    return await (await value.call(*args)).to_python()


async def _nullary_call(value: Any) -> Any:
    """``f()`` must be refused identically. Nix has no nullary application."""
    return await value.call()


async def _chained_selection(value: Any) -> Any:
    """Two selections, one expression, no await between them.

    The shape LEDGER's "Value.attr:async" made impossible to write once.
    """
    return await value.attr("a").attr("b").list_get(1).to_python()


async def _apply_as_string(value: Any, function: str) -> str:
    """``apply`` applies *function* to this value -- the value is the argument."""
    return await (await value.apply(function)).as_string()


async def _select_after_release(value: Any) -> Any:
    """Select off a value that has been released.

    A *lifetime* failure rather than a Nix one -- Nix is never consulted, so
    it belongs to ``ObjectLifetimeError`` and not ``NixError``. inproc used to
    raise its own ``InprocValueReleasedError``, which no ``except`` written
    against rpc could catch; both raise ``ValueReleasedError`` now.

    Selection specifically, because it is the operation that got lazy: the
    check has to fire here rather than at whatever eventually forces the
    chain.
    """
    await value.release()
    return value.attr("a")


async def _force_after_release(value: Any) -> Any:
    """The forced half of :func:`_select_after_release`, on the value itself."""
    await value.release()
    return await value.get_type()


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
    # as_dict/as_list hand back live handles, so compare what they resolve to
    # rather than the handles themselves.
    Case("as_dict_keys", "{ b = 1; a = 2; }", _as_dict_keys),
    Case("as_dict_values", "{ a = 1; b = 2; }", _as_dict_values),
    Case("as_list_values", "[10 20 30]", _as_list_values),
    Case("as_dict_empty", "{}", _as_dict_keys),
    Case("as_list_empty", "[]", _as_list_values),
    # The shape to_python() cannot flatten on either engine: a Nix function has
    # no JSON form. as_dict() reads it one level at a time instead, which is
    # what makes nanopynix's own parseNetwork result usable from Python.
    Case("as_dict_over_a_function_valued_attrset", "{ n = 1; f = x: x; }", _as_dict_keys),
    Case("apply_a_function_reached_through_as_dict", "{ f = x: x + 1; n = 41; }", _apply_via_as_dict),
    # get_type() is the forcing verb: evaluate to WHNF, answer with what that
    # turned out to be. It replaced force() -- and with it the force_attrs /
    # force_list ledger entries, since the disagreement was entirely about what
    # a *forced compound* should come back as, a question neither engine is
    # asked any more. Every type is covered here precisely because those two
    # are where the engines used to part company.
    Case("get_type_int", "1 + 2", lambda v: v.get_type()),
    Case("get_type_string", '"hi"', lambda v: v.get_type()),
    Case("get_type_bool", "true", lambda v: v.get_type()),
    Case("get_type_null", "null", lambda v: v.get_type()),
    Case("get_type_attrs", "{ a = 1; }", lambda v: v.get_type()),
    Case("get_type_list", "[1 2]", lambda v: v.get_type()),
    Case("get_type_function", "x: x", lambda v: v.get_type()),
    # Selection is lazy on both engines, so an absent one is not an error yet.
    Case("attr_missing_is_deferred", "{ x = 1; }", lambda v: _selection_defers(v.attr, "nope")),
    Case("list_index_out_of_range_is_deferred", "[1 2 3]", lambda v: _selection_defers(v.list_get, 99)),
    Case("chained_selection", "{ a = { b = [10 20]; }; }", _chained_selection),
    Case("call_one_argument", "x: x + 1", lambda v: _curried_call(v, 41)),
    Case("call_two_arguments", "x: y: x + y", lambda v: _curried_call(v, 40, 2)),
    Case("call_three_arguments", "x: y: z: x + y + z", lambda v: _curried_call(v, 1, 2, 3)),
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
    # The absence pair, forced: both must be Nix errors *and* Python ones,
    # identically on both engines. Their unforced halves live in SUCCESS_CASES
    # and assert the opposite -- that neither engine raises before forcing.
    Case("attr_missing_forced", "{ x = 1; }", lambda v: _attr_then_force(v, "nope")),
    Case("list_index_out_of_range_forced", "[1 2 3]", lambda v: _list_get_then_force(v, 99)),
    # Strict accessors on the wrong type.
    Case("as_int_on_string", '"nope"', lambda v: v.as_int()),
    Case("as_string_on_int", "42", lambda v: v.as_string()),
    Case("as_bool_on_null", "null", lambda v: v.as_bool()),
    # to_python() has no answer for a function, on either engine.
    Case("to_python_of_a_function", "x: x", lambda v: v.to_python()),
    Case("to_python_of_an_attrset_holding_a_function", "{ f = x: x; }", lambda v: v.to_python()),
    # Applying something that is not a function at all.
    Case("apply_a_non_function", "42", lambda v: v.apply("1 + 1")),
    Case("call_with_no_arguments", "x: x", _nullary_call),
    Case("call_a_non_function", "42", lambda v: _curried_call(v, 1)),
    Case("call_too_many_arguments", "x: x", lambda v: _curried_call(v, 1, 2)),
    # Lifetime, not Nix: the object is gone, so the failure is nanopynix's own
    # and must still arrive as one class on both engines.
    Case("select_after_release", "{ a = 1; }", _select_after_release),
    Case("force_after_release", "{ a = 1; }", _force_after_release),
    # Navigation accessors must refuse the wrong type rather than answering.
    Case("attr_names_on_an_int", "1", _sorted_attr_names),
    Case("list_length_on_an_attrset", "{}", lambda v: v.list_length()),
    Case("has_attr_on_a_list", "[]", lambda v: v.has_attr("a")),
]


# Cases where the engines are known *not* to agree, and why. Same discipline
# as test_engine_parity.py's LEDGER: an entry is a defect that is counted, not
# an exemption granted. The test below asserts these still disagree, so an
# entry cannot outlive the divergence it documents.
# Empty, and the guard below is kept anyway: it is the mechanism for the next
# entry, and an empty dict is the state this file exists to reach. Its last two
# entries were "attr_missing" and "list_index_out_of_range", the behavioural
# cost of LEDGER's "Value.attr:async" -- inproc raised on selection while rpc
# deferred to the force. Both defer now, so both cases moved to SUCCESS_CASES.
SEMANTIC_LEDGER: dict[str, str] = {}


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
    """The same expression and operation must return the same Python value.

    Both halves are asserted, and the "did it succeed" half is not redundant:
    the two engines share more code than they used to, so a break in the
    shared layer makes *both* raise and equality alone would read that as
    agreement. The failure half below has always had the mirror of this
    assertion; this side was missing it.
    """
    inproc_outcome = await _run(inproc_session, case)
    rpc_outcome = await _run(rpc_session, case)
    if case.name in SEMANTIC_LEDGER:
        assert inproc_outcome != rpc_outcome, (
            f"{case.name!r} is in SEMANTIC_LEDGER but the engines now agree ({inproc_outcome!r}) -- delete the entry"
        )
        return
    assert inproc_outcome[0] == "value", f"inproc raised: {inproc_outcome!r}"
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
            f"{case.name!r} is in SEMANTIC_LEDGER but the engines now agree ({inproc_outcome!r}) -- delete the entry"
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


# ── Evaluator construction parity ────────────────────────────────────
#
# Not expressible as a Case: these are about the arguments an evaluator can be
# *built* with, before any expression runs. They retire two LEDGER DEFECTs
# ("Session.eval:params" and the build_store half of "Session.repl:params").


async def _evaluates_with_a_separate_build_store(factory: Any) -> str:
    """Open an evaluator against one store while building into another."""
    async with (
        factory() as session,
        session.store() as store,
        session.store() as build_store,
        session.eval(store, build_store=build_store) as evaluator,
    ):
        return await (await evaluator.string('"reached the evaluator"')).as_string()


async def _repl_honours_a_per_call_line_editor(factory: Any) -> tuple[str, ...]:
    async with (
        factory() as session,
        session.store() as store,
        session.repl(store, line_editors=("nanopynix-parity-probe",)) as repl,
    ):
        return tuple(repl.line_editors)


async def test_both_engines_evaluate_against_one_store_and_build_into_another(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """``eval(store, build_store=...)`` must be accepted and usable on both.

    rpc could not express this at all: ``OpenEvalRequest`` carried no
    ``build_store_handle`` and ``_worker_eval.open_eval`` passed a hardcoded
    ``None`` into ``open_eval_state``, whose third parameter is the build
    store. inproc has always taken the pair. Nothing about a subprocess
    boundary required the asymmetry -- ``store.proto`` already ships an
    optional ``eval_store_handle`` for ``BuildPathsWithResults``, so the wire
    encoding was a solved problem sitting one file away.

    What this asserts is exactly "accepted and usable", and no more. Both
    stores here come from the same session and resolve to the same underlying
    Nix store, so evaluation would succeed even if the handle were dropped --
    measured: reverting the worker to its hardcoded ``None`` leaves this test
    green. The plumbing witnesses are
    ``test_worker_eval_unit.test_open_eval_forwards_the_build_store_handle``
    and ``test_session_unit.test_open_eval_carries_the_build_store_handle``,
    one per side of the wire; both go red under that same experiment. Stated
    here so this test is not mistaken for the gate it is not.
    """
    assert await _evaluates_with_a_separate_build_store(inproc_session) == "reached the evaluator"
    assert await _evaluates_with_a_separate_build_store(rpc_session) == "reached the evaluator"


async def _resolved_flake_ref(factory: Any, ref: str) -> dict[str, Any]:
    """Resolve a flake ref through the evaluator, without evaluating outputs."""
    async with (
        factory() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        resolved = await evaluator.get_flake(ref)
        return {name: value.to_dict() for name, value in sorted(resolved.attrs.items())}


async def test_both_engines_resolve_a_flake_ref_without_evaluating_it(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
    tmp_path: Path,
) -> None:
    """``get_flake`` must resolve identically on both engines.

    It was rpc-only, though it is pure libexpr plus a registry lookup -- the
    worker held the whole implementation inline (parse the ref, call
    ``nanopynix_flake.get_flake``, extract the attrs) rather than in the
    ``CoreEvalState`` both engines share. Moving those three steps down is what
    let inproc have it, and is why this asserts the *resolved attrs* rather
    than merely that neither engine raised: the extraction is the part that
    could have diverged.
    """
    (tmp_path / "flake.nix").write_text("{ outputs = _: { probe = 1; }; }\n")
    ref = f"path:{tmp_path}"
    assert await _resolved_flake_ref(inproc_session, ref) == await _resolved_flake_ref(rpc_session, ref)


async def test_both_engines_take_line_editors_per_repl(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """``repl(line_editors=...)`` is per-call on both engines.

    rpc read it only from ``Session.runtime_settings``, so two REPLs in one
    worker could not use different editors -- a caller wanting that had to open
    a second worker. It describes one interactive front-end, which is what
    inproc's per-call argument already said.
    """
    probe = ("nanopynix-parity-probe",)
    assert await _repl_honours_a_per_call_line_editor(inproc_session) == probe
    assert await _repl_honours_a_per_call_line_editor(rpc_session) == probe


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


# ── Object lifetime parity ───────────────────────────────────────────
#
# Not expressible as a Case: a Case is handed a value, and three of the four
# things with a lifetime are not values. These are failures nanopynix raises
# on its own, without consulting Nix, so they are ObjectLifetimeError rather
# than NixError -- and until this, inproc raised its own InprocSessionClosedError
# /InprocValueReleasedError/InprocLockedFlakeReleasedError, none of which an
# `except` written against the rpc engine could catch. Neither ledger could
# see it: the classes have different *names*, so a name-based comparison never
# put them side by side.
#
# Four things have a lifetime and all four are covered. The `Session` came
# last, and separately, because it was not a mismatched class but a missing
# guard: rpc tracked no open/closed state, so work dispatched after `close()`
# failed as whatever transport error the dead channel produced. Both engines
# now refuse it at their dispatch chokepoint -- `inproc.Session.run`'s
# `_check_open`, `WorkerClient.invoke`'s -- rather than at `store()`/`eval()`/
# `repl()`, which on either engine hand back an object whose first piece of
# work is what fails.


async def _use_a_closed_session(factory: Any) -> str:
    """Ask a closed session to do work of its own.

    ``get_verbosity`` because it is the shortest path to each engine's dispatch
    chokepoint: inproc's routes through ``Session.run``, rpc's through
    ``WorkerClient.invoke``, which is where both guards live.
    """
    async with factory() as session:
        pass
    try:
        await session.get_verbosity()
    except Exception as exc:
        return type(exc).__name__
    return "no exception"


async def _use_a_store_that_outlived_its_session(factory: Any) -> str:
    """Use a store facade whose session has since closed.

    The shape a caller actually gets into, and a different answer from the one
    above: closing a session closes the stores it handed out, so the honest
    complaint is about the store and not the session. rpc left them open until
    the session learned to close them, and reported "session is not open" here.
    """
    async with factory() as session:
        store = session.store()
        await store.open()
    try:
        await store.uri()
    except Exception as exc:
        return type(exc).__name__
    return "no exception"


async def _use_a_closed_store(factory: Any) -> str:
    async with factory() as session:
        store = session.store()
        await store.open()
        await store.close()
        try:
            await store.uri()
        except Exception as exc:
            return type(exc).__name__
        return "no exception"


async def _use_a_closed_evaluator(factory: Any) -> str:
    async with factory() as session, session.store() as store:
        evaluator = session.eval(store)
        await evaluator.open()
        await evaluator.close()
        try:
            await evaluator.string("1")
        except Exception as exc:
            return type(exc).__name__
        return "no exception"


async def _use_a_released_locked_flake(factory: Any, ref: str) -> str:
    async with factory() as session, session.store() as store, session.eval(store) as evaluator:
        locked = await evaluator.lock_flake(ref, write_lock_file=False)
        await locked.release()
        try:
            await locked.eval()
        except Exception as exc:
            return type(exc).__name__
        return "no exception"


async def test_both_engines_name_a_closed_session_the_same_way(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """rpc had no guard here at all; the failure was whatever the dead channel gave."""
    assert await _use_a_closed_session(inproc_session) == "SessionClosedError"
    assert await _use_a_closed_session(rpc_session) == "SessionClosedError"


async def test_both_engines_name_a_store_outliving_its_session_the_same_way(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """A closed session leaves closed stores behind, not merely unusable ones."""
    assert await _use_a_store_that_outlived_its_session(inproc_session) == "StoreClosedError"
    assert await _use_a_store_that_outlived_its_session(rpc_session) == "StoreClosedError"


async def test_both_engines_name_a_closed_store_the_same_way(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """rpc raised a bare ``RuntimeError`` here, which nothing could catch precisely."""
    assert await _use_a_closed_store(inproc_session) == "StoreClosedError"
    assert await _use_a_closed_store(rpc_session) == "StoreClosedError"


async def test_both_engines_name_a_closed_evaluator_the_same_way(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    assert await _use_a_closed_evaluator(inproc_session) == "EvalSessionClosedError"
    assert await _use_a_closed_evaluator(rpc_session) == "EvalSessionClosedError"


async def test_both_engines_name_a_released_locked_flake_the_same_way(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
    tmp_path: Path,
) -> None:
    """A locked flake is not a value, so it does not raise ``ValueReleasedError``.

    rpc did, reusing the value class for a flake handle; a caller releasing
    values in a loop would have had that ``except`` swallow a flake handle it
    never meant to touch. inproc drew the distinction first and it is the one
    kept.
    """
    (tmp_path / "flake.nix").write_text("{ outputs = _: { probe = 1; }; }\n")
    ref = f"path:{tmp_path}"
    assert await _use_a_released_locked_flake(inproc_session, ref) == "LockedFlakeReleasedError"
    assert await _use_a_released_locked_flake(rpc_session, ref) == "LockedFlakeReleasedError"


# ── Log capture parity ───────────────────────────────────────────────


async def _capture_a_trace(factory: Any, expression: str) -> tuple[list[str], bool]:
    """Capture log events around an evaluation, on either engine.

    Returns the recorded Nix log actions and whether every dispatched
    operation was tagged. The second half is the part that is easy to get
    wrong and invisible from the first: a capture that subscribes to the bus
    but never learns which operations it started exits immediately, before the
    work it was capturing has finalized, and still hands back a
    plausible-looking list.
    """
    async with factory() as session, session.store() as store, session.eval(store) as evaluator:
        async with session.capture_logs() as logs:
            await (await evaluator.string(expression)).to_python()
        return ([event.action for event in logs.events], bool(logs.request_ids))


async def test_both_engines_capture_logs_the_same_way(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """``capture_logs()`` must exist and record identically on both engines.

    It was rpc-only -- LEDGER's "Session.capture_logs:rpc-only" -- with
    ``LogCapture`` living in ``rpc.client.session`` over a ``ContextVar`` in
    ``rpc.client._pool``, though nothing about recording log events depends on
    where Nix runs. inproc already had the bus, the operation ids and the
    ``request_finalized`` events; what it lacked was the class and its half of
    the tagging contract.

    ``builtins.trace`` because it emits exactly one event, at the default
    verbosity, for a pure evaluation -- no store or build activity, so nothing
    in the count depends on what is already in the store.

    Asserting ``request_ids`` is non-empty rather than only that the block ran
    is deliberate: without inproc's tagging hook the capture still yields and
    still collects the event, and only this half notices.
    """
    expression = 'builtins.trace "nanopynix parity trace" 1'
    assert await _capture_a_trace(inproc_session, expression) == (["msg"], True)
    assert await _capture_a_trace(rpc_session, expression) == (["msg"], True)
