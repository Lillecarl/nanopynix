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

from nanopynix import NixError, NixType, NixTypeError, WrongNixTypeError

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
# `PyValue::to_python()`, which meant force()/force_deep() raised
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
    """force/force_deep/as_string all accept an interpolated derivation, and agree."""
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(CONTEXT_STRING_EXPR)
        attr = await value.attr("s")
        forced = await attr.force()
        assert _is_store_path(forced), forced
        assert await attr.as_string() == forced
        assert await value.force_deep() == {"s": forced}


async def test_rpc_reads_a_string_carrying_store_path_context(rpc_session: RpcSessionFactory) -> None:
    """The same, over the wire -- the worker converts with the same binding."""
    async with rpc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(CONTEXT_STRING_EXPR)
        attr = value.attr("s")
        forced = await attr.force()
        assert _is_store_path(forced), forced
        assert await attr.as_string() == forced
        assert await value.force_deep() == {"s": forced}


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
