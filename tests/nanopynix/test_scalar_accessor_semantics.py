"""Pin how a Nix value becomes a Python value, on both engines.

Two things, deliberately kept separate:

``as_int``/``as_float``/``as_bool``/``as_string`` are the FFI boundary -- they
assert the value already has that type and raise ``NixTypeError`` otherwise.
They are the one thing no Nix expression can do for you, since something has to
hand back an actual Python object.

Everything else is a Nix operation, so ``apply()`` runs the Nix function rather
than reimplementing it. There used to be a ``coerce_str``/``coerce_int``/
``coerce_float``/``coerce_bool`` family; ``coerce_str`` was ``builtins.toString``
spelled a second way (and got it wrong -- it returned ``"true"`` where Nix says
``"1"``), and the other three had no Nix counterpart at all.

These tables exist so that "unifying" or reintroducing a coercion helper cannot
silently disagree with Nix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from nanopynix import NixError, NixEvalSettings, NixType, NixTypeError, WrongNixTypeError

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.support.nix_environment import InprocSessionFactory, RpcSessionFactory

# A sentinel meaning "this accessor must raise its family's error for this value".
RAISES = object()

# expression -> {accessor: expected value or RAISES}
#
# Nothing here converts: a string is not an int, an int is not a string.
# Compare NIX_TOSTRING below, where `42` does become `"42"` -- because that
# is Nix's coercion, reached through apply(), not through an accessor.
STRICT_TABLE: dict[str, dict[str, Any]] = {
    '"42"': {"as_int": RAISES, "as_float": RAISES, "as_bool": RAISES, "as_string": "42"},
    # forceFloat accepts an int, so as_float widens 42 -> 42.0; forceInt does
    # not narrow, so as_int rejects a float even when it is integral.
    "42": {"as_int": 42, "as_float": 42.0, "as_bool": RAISES, "as_string": RAISES},
    "42.0": {"as_int": RAISES, "as_float": 42.0, "as_bool": RAISES, "as_string": RAISES},
    "true": {"as_int": RAISES, "as_float": RAISES, "as_bool": True, "as_string": RAISES},
    # No leniency anywhere in this family, including null -> bool. Use
    # is_null(), or apply("builtins.toString"), when a null should mean
    # something other than an error.
    "null": {"as_int": RAISES, "as_float": RAISES, "as_bool": RAISES, "as_string": RAISES},
}

# The navigation accessors are strict the same way, which they were not.
#
# They each used to answer for the wrong type rather than refuse:
# ``attr_names()`` returned ``[]`` for an int, ``has_attr()`` returned ``False``
# for a function, and ``list_length()`` returned ``0`` for an attrset -- so
# ``for i in range(await v.list_length())`` was a silent no-op on an attrset,
# which surfaces as wrong output much later instead of as an exception here.
# Identically on both engines, so the parity harness saw nothing wrong.
#
# The one non-obvious row is `list_length` on an attrset and `attr_names` on a
# list: adjacent compound types are still the wrong type.
NAVIGATION_TABLE: dict[str, dict[str, Any]] = {
    "42": {"attr_names": RAISES, "has_attr": RAISES, "list_length": RAISES},
    '"s"': {"attr_names": RAISES, "has_attr": RAISES, "list_length": RAISES},
    "null": {"attr_names": RAISES, "has_attr": RAISES, "list_length": RAISES},
    "x: x": {"attr_names": RAISES, "has_attr": RAISES, "list_length": RAISES},
    "{ a = 1; }": {"attr_names": ["a"], "has_attr": True, "list_length": RAISES},
    "[ 1 2 ]": {"attr_names": RAISES, "has_attr": RAISES, "list_length": 2},
}


async def _check_navigation(value: Any, expectations: dict[str, Any], engine: str) -> None:
    for name, expected in expectations.items():
        accessor = value.has_attr("a") if name == "has_attr" else getattr(value, name)()
        label = f"{engine}.{name}"
        if expected is RAISES:
            with pytest.raises(NixTypeError):
                await accessor
        else:
            assert await accessor == expected, label


@pytest.mark.parametrize("expr", list(NAVIGATION_TABLE), ids=list(NAVIGATION_TABLE))
async def test_inproc_navigation_accessors_are_strict(expr: str, inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        await _check_navigation(await ev.string(expr), NAVIGATION_TABLE[expr], "inproc")


@pytest.mark.parametrize("expr", list(NAVIGATION_TABLE), ids=list(NAVIGATION_TABLE))
async def test_rpc_navigation_accessors_are_strict(expr: str, rpc_session: RpcSessionFactory) -> None:
    async with rpc_session() as session, session.store() as store, session.eval(store) as ev:
        await _check_navigation(await ev.string(expr), NAVIGATION_TABLE[expr], "rpc")

# `apply("builtins.toString")` is Nix's own string coercion. These
# expectations were taken from a real `nix eval` of builtins.toString over the
# same expressions, so they fail rather than drift if Nix ever changes them.
#
# The last four cases are the ones that matter: no plausible Python
# reimplementation joins lists on a space or honours `__toString`/`outPath`.
# They are why there is no hand-written coercion here to get wrong.
NIX_TOSTRING: dict[str, str] = {
    '"x"': "x",
    "42": "42",
    "-42": "-42",
    "42.5": "42.500000",
    # true is "1" but false is "" -- not "true"/"false", and not symmetric.
    "true": "1",
    "false": "",
    "null": "",
    "[ 1 2 ]": "1 2",
    '[ "a" "b" ]': "a b",
    '{ __toString = self: "custom"; }': "custom",
    '{ outPath = "/nix/store/xxx"; }': "/nix/store/xxx",
}

# Nix will not stringify these, and neither do we.
NIX_TOSTRING_REJECTS = ["{ a = 1; }", "x: x"]


async def _check(
    accessor: Callable[[], Any],
    expected: Any,
    error: type[Exception],
    label: str,
) -> None:
    if expected is RAISES:
        with pytest.raises(error):
            await accessor()
        return
    result = await accessor()
    assert result == expected, label
    assert type(result) is type(expected), f"{label}: expected {type(expected).__name__}, got {type(result).__name__}"


@pytest.mark.parametrize("expr", list(STRICT_TABLE), ids=list(STRICT_TABLE))
async def test_inproc_as_accessors_are_strict(expr: str, inproc_session: InprocSessionFactory) -> None:
    """``as_*`` asserts the value already has the type -- it never converts."""
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(expr)
        for name, expected in STRICT_TABLE[expr].items():
            await _check(getattr(value, name), expected, NixTypeError, f"{expr}.{name}")


@pytest.mark.parametrize("expr", list(STRICT_TABLE), ids=list(STRICT_TABLE))
async def test_rpc_as_accessors_are_strict_the_same_way(expr: str, rpc_session: RpcSessionFactory) -> None:
    """rpc's ``as_*`` accepts, rejects, and *raises* exactly as inproc's does.

    Same table, same expected ``NixTypeError`` -- not merely "both fail". The
    type check runs in the worker so the error is Nix's own, which is the only
    way the two engines agree on the exception type and its message.
    """
    async with rpc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(expr)
        for name, expected in STRICT_TABLE[expr].items():
            await _check(getattr(value, name), expected, NixTypeError, f"{expr}.{name}")


@pytest.mark.parametrize("expr", list(NIX_TOSTRING), ids=list(NIX_TOSTRING))
async def test_inproc_apply_tostring_is_nix_tostring(expr: str, inproc_session: InprocSessionFactory) -> None:
    """String coercion comes from Nix's builtin, so it cannot be an approximation."""
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(expr)
        assert await (await value.apply("builtins.toString")).as_string() == NIX_TOSTRING[expr]


@pytest.mark.parametrize("expr", list(NIX_TOSTRING), ids=list(NIX_TOSTRING))
async def test_rpc_apply_tostring_is_nix_tostring(expr: str, rpc_session: RpcSessionFactory) -> None:
    """rpc agrees with inproc, because neither one implements the coercion."""
    async with rpc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(expr)
        assert await (await value.apply("builtins.toString")).as_string() == NIX_TOSTRING[expr]


@pytest.mark.parametrize("expr", NIX_TOSTRING_REJECTS, ids=NIX_TOSTRING_REJECTS)
async def test_apply_tostring_rejects_what_nix_rejects(
    expr: str,
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """A value Nix will not stringify is an error on both engines, not a fallback."""
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        with pytest.raises(NixError):
            await (await (await ev.string(expr)).apply("builtins.toString")).as_string()

    async with rpc_session() as session, session.store() as store, session.eval(store) as ev:
        with pytest.raises(NixError):
            await (await (await ev.string(expr)).apply("builtins.toString")).as_string()


# apply() is not a stringification helper -- it is the one door to every
# builtin. These are the cases a dedicated coerce_* family could never cover.
APPLY_CASES = [
    ("[ 1 2 3 ]", "builtins.length", "as_int", 3),
    ("[ 1 2 3 ]", "builtins.typeOf", "as_string", "list"),
    ("[ 1 2 3 ]", "builtins.toJSON", "as_string", "[1,2,3]"),
    ("[ 1 2 3 ]", "xs: builtins.elemAt xs 1", "as_int", 2),
    ("{ a = 1; b = 2; }", "builtins.attrNames", "apply-length", 2),
    ('"hello"', "builtins.stringLength", "as_int", 5),
]


@pytest.mark.parametrize(
    ("expr", "function", "accessor", "expected"),
    APPLY_CASES,
    ids=[f"{f}" for _, f, _, _ in APPLY_CASES],
)
async def test_apply_reaches_any_builtin(
    expr: str,
    function: str,
    accessor: str,
    expected: object,
    inproc_session: InprocSessionFactory,
) -> None:
    """One method, the whole of builtins, plus arbitrary lambdas."""
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(expr)
        applied = await value.apply(function)
        if accessor == "apply-length":
            assert await (await applied.apply("builtins.length")).as_int() == expected
        else:
            assert await getattr(applied, accessor)() == expected


async def test_apply_accepts_an_already_evaluated_function(inproc_session: InprocSessionFactory) -> None:
    """Hoisting the function out of a loop is what a memoising apply() would buy.

    Passing the evaluated function instead of its source means one evaluation
    for many values, without apply() owning a cache or its lifetime.
    """
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        to_string = await ev.string("builtins.toString")
        for expr, expected in (("1", "1"), ("true", "1"), ("null", "")):
            value = await ev.string(expr)
            assert await (await value.apply(to_string)).as_string() == expected


# An interpolated derivation is a string that carries store-path context. It is
# also the single most common shape of Nix string a caller will ever hold, so
# every path that turns a Nix string into a Python str has to accept it.
#
# `forceStringNoCtx` -- the accessor Nix uses where context would be *unsound*,
# such as an import path -- rejects exactly this. It was wired into
# `PyValue::to_python()`, which meant force()/to_python() raised
# "the string '/nix/store/...' is not allowed to refer to a store path" on both
# engines while as_string() on the same value succeeded. Dropping the context is
# the only honest option here: a Python str cannot carry one.
CONTEXT_STRING_EXPR = """
  let
    drv = builtins.derivation {
      name = "context-string";
      system = builtins.currentSystem;
      builder = "/bin/sh";
    };
  in { s = "${drv}"; }
"""


def _is_store_path(value: object) -> bool:
    return isinstance(value, str) and value.startswith("/nix/store/") and value.endswith("-context-string")


async def test_inproc_reads_a_string_carrying_store_path_context(inproc_session: InprocSessionFactory) -> None:
    """force/to_python/as_string all accept an interpolated derivation, and agree."""
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(CONTEXT_STRING_EXPR)
        attr = await value.attr("s")
        forced = await attr.force()
        assert _is_store_path(forced), forced
        assert await attr.as_string() == forced
        assert await value.to_python() == {"s": forced}


async def test_rpc_reads_a_string_carrying_store_path_context(rpc_session: RpcSessionFactory) -> None:
    """The same, over the wire -- the worker converts with the same binding."""
    async with rpc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(CONTEXT_STRING_EXPR)
        attr = value.attr("s")
        forced = await attr.force()
        assert _is_store_path(forced), forced
        assert await attr.as_string() == forced
        assert await value.to_python() == {"s": forced}


# Flattening a whole value tree into Python data is `builtins.toJSON`'s job, and
# these expectations come from a real `nix eval --json` of each expression.
#
# The two attrset shortcuts are the load-bearing part. `__toString` wins over
# `outPath`, and `outPath` collapses the attrset to that one string -- which is
# what makes a *derivation* convertible at all, since `drv.out`/`drv.all`/
# `drv.drvAttrs` point back at `drv` and a naive walk never terminates. Note the
# rule keys off `outPath`, not off `type = "derivation"`.
NIX_TOJSON: dict[str, object] = {
    '{ a = 1; b = [ 1 "two" ]; }': {"a": 1, "b": [1, "two"]},
    '{ __toString = self: "custom"; }': "custom",
    '{ __toString = self: "custom"; outPath = "/foo"; }': "custom",
    '{ outPath = "/foo"; a = 1; }': "/foo",
    # No outPath, so the type tag alone changes nothing.
    '{ type = "derivation"; a = 1; }': {"a": 1, "type": "derivation"},
}

# Nix refuses to flatten these, and so must we -- as an error, never as a
# placeholder. The hand-rolled walk used to return the *name* of the type, so a
# lambda quietly became the string "function".
NIX_TOJSON_REJECTS = ["x: x", "{ f = x: x; }"]


@pytest.mark.parametrize("expr", list(NIX_TOJSON), ids=list(NIX_TOJSON))
async def test_inproc_deep_conversion_follows_nix_tojson(expr: str, inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        assert await (await ev.string(expr)).to_python() == NIX_TOJSON[expr]


@pytest.mark.parametrize("expr", NIX_TOJSON_REJECTS, ids=NIX_TOJSON_REJECTS)
async def test_inproc_deep_conversion_rejects_a_function(expr: str, inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        with pytest.raises(NixError):
            await (await ev.string(expr)).to_python()


async def test_deep_conversion_of_a_derivation_terminates(inproc_session: InprocSessionFactory) -> None:
    """A derivation is a cyclic attrset; converting one used to take SIGSEGV.

    ``drv.out`` is ``drv`` and ``drv.all`` is ``[ drv ]``, so a walk with no
    ``outPath`` shortcut recurses until the C++ stack runs out and kills the
    interpreter -- not an exception, a crash. Nix's own converter stops at the
    output path, which is what this asserts.
    """
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(
            'builtins.derivation { name = "deep-cyclic"; system = builtins.currentSystem; builder = "/bin/sh"; }',
        )
        result = await value.to_python()
        assert isinstance(result, str), result
        assert result.startswith("/nix/store/"), result
        assert result.endswith("-deep-cyclic"), result


async def test_deep_conversion_of_a_true_cycle_raises_instead_of_crashing(
    inproc_session: InprocSessionFactory,
) -> None:
    """A cycle with no ``outPath`` to stop at must still be an error, not SIGSEGV.

    Nix answers this with "stack overflow; max-call-depth exceeded", which it
    raises as ``nix::StackOverflowError``. That derives from
    ``nix::EvalBaseError`` rather than ``nix::EvalError``, so before
    ``EvalBaseError`` was registered it matched no exception translator and
    arrived here as a bare ``RuntimeError`` carrying no ``ErrorInfo`` at all.
    Hence the two assertions beyond the message: the type, and that Nix's own
    structured detail survived the boundary.
    """
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string("let x = { y = x; }; in x")
        with pytest.raises(NixError, match="max-call-depth") as excinfo:
            await value.to_python()
        assert excinfo.value.info is not None


async def test_max_call_depth_is_a_nix_error_with_error_info(inproc_session: InprocSessionFactory) -> None:
    """``nix::StackOverflowError`` must not bypass the ``NixError`` hierarchy.

    The direct test for the same mapping the cycle above reaches incidentally,
    and the one that pins it: it drives the guard through plain recursion at an
    explicit ``max_call_depth`` rather than relying on Nix's default of 10000.
    A low explicit limit reaches the guard with room to spare no matter how big
    the evaluator's stack is; the two tests below cover the default depth,
    which is the case that depends on the stack being sized correctly.

    ``StackOverflowError`` derives from ``nix::EvalBaseError``, not
    ``nix::EvalError``, so it is only bound because ``EvalBaseError`` itself is
    registered. ``IFDError`` and ``RecoverableEvalError`` sit in the same
    position and ride on the same registration.
    """
    settings = NixEvalSettings(max_call_depth=1000)
    async with (
        inproc_session() as session,
        session.store() as store,
        session.eval(store, eval_settings=settings) as ev,
    ):
        # ev.string() already forces to WHNF, so the guard fires here rather
        # than at a later accessor.
        with pytest.raises(NixError, match="max-call-depth") as excinfo:
            await ev.string("let f = n: f (n + 1); in f 0")
        # Nix's structured detail is the whole point: C++ is the only place
        # that has the source position, so losing it is unrecoverable. The
        # position, not the trace -- Nix throws this one at the offending call
        # with no frames accumulated yet, so `traces` is legitimately empty.
        info = excinfo.value.info
        assert info is not None
        assert info["pos"] is not None


# The canonical Nix mistake. At Nix's *default* max-call-depth of 10000 this
# needs roughly 27 MB of C stack, which is the whole point of the two tests
# below: a thread inherits 8 MiB from RLIMIT_STACK, so the stack used to run
# out thousands of frames before Nix's counter could fire. That was not an
# ugly error, it was SIGSEGV -- it killed the host process outright on inproc,
# and killed the worker (`WorkerDiedError: Connection lost`) on rpc.
#
# The evaluator thread now gets the 60 MiB `nix::initNix()` asks for; see
# NIX_EVALUATOR_STACK_SIZE. A regression here does not fail these tests, it
# aborts the whole pytest process -- which is exactly why they exist.
RUNAWAY_RECURSION = "let f = n: f (n + 1); in f 0"


async def test_inproc_reports_runaway_recursion_at_nixs_default_depth(
    inproc_session: InprocSessionFactory,
) -> None:
    """Nix's own counter must win the race against the C stack, not lose it."""
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        with pytest.raises(NixError, match="max-call-depth") as excinfo:
            await ev.string(RUNAWAY_RECURSION)
        assert excinfo.value.info is not None


async def test_rpc_reports_runaway_recursion_at_nixs_default_depth(
    rpc_session: RpcSessionFactory,
) -> None:
    """The same, in the worker -- it sizes its evaluator thread the same way."""
    async with rpc_session() as session, session.store() as store, session.eval(store) as ev:
        with pytest.raises(NixError, match="max-call-depth") as excinfo:
            await ev.string(RUNAWAY_RECURSION)
        assert excinfo.value.info is not None


# force_as is the generic by-NixType entry point and still covers what no
# as_* does (attrs/list/function). Bool is omitted here only because the
# table's null row would need a second expected exception.
FORCE_AS_TYPES = [
    ("as_int", NixType.INT),
    ("as_float", NixType.FLOAT),
    ("as_string", NixType.STRING),
]

# (expr, accessor) pairs where force_as does NOT match inproc's as_*, with why.
#
# force_as compares `typeOf` for equality, so an int is not a float. inproc's
# as_float goes through nix's own `forceFloat`, which accepts nInt and widens
# -- the same rule that makes `1 + 1.0` work in Nix. So on this one cell rpc is
# stricter than Nix itself. Which side should move is part of the open decision
# in TODO.md; this pins the current answer so the change cannot be silent.
FORCE_AS_DIVERGENCES = {
    ("42", "as_float"): "force_as(FLOAT) rejects an int; nix's forceFloat widens it",
}


@pytest.mark.parametrize(("accessor", "nix_type"), FORCE_AS_TYPES, ids=[name for name, _ in FORCE_AS_TYPES])
@pytest.mark.parametrize("expr", ['"42"', "42", "42.0", "true", "null"])
async def test_force_as_is_the_rpc_spelling_of_the_strict_accessor(
    accessor: str,
    nix_type: NixType,
    expr: str,
    rpc_session: RpcSessionFactory,
) -> None:
    """rpc's ``force_as(NixType.X)`` accepts and rejects what inproc's ``as_x`` does, bar one cell.

    This near-equivalence is what makes ``force_as`` -- not ``coerce_*`` -- the
    thing to unify ``as_*`` with. The one exception is tabulated above rather
    than smoothed over, because it is a genuine semantic difference and not a
    naming one.
    """
    expected = STRICT_TABLE[expr][accessor]
    diverges = (expr, accessor) in FORCE_AS_DIVERGENCES

    async with rpc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(expr)
        if expected is RAISES or diverges:
            with pytest.raises(WrongNixTypeError):
                await value.force_as(nix_type)
        else:
            assert await value.force_as(nix_type) == expected
