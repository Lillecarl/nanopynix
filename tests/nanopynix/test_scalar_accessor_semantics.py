"""Pin what the two scalar-accessor families actually do, on both engines.

inproc's ``as_int``/``as_float``/``as_bool``/``as_string`` and rpc's
``coerce_int``/``coerce_float``/``coerce_bool``/``coerce_str`` were once
recorded as one concept spelled two ways. They are not: ``as_*`` are strict
type assertions, ``coerce_*`` convert. rpc's strict counterpart is
``force_as(NixType.X)``, and inproc has no coercing accessor at all.

That distinction is invisible from the names, so it is easy to "unify" the
two families into one and silently change behaviour for every caller. These
tables exist to make that impossible without a failing test. See TODO.md's
"the two scalar-accessor families are not one concept" for the open decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from nanopynix import NixCoercionError, NixError, NixType, NixTypeError, WrongNixTypeError

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.support.nix_environment import InprocSessionFactory, RpcSessionFactory

# A sentinel meaning "this accessor must raise its family's error for this value".
RAISES = object()

# expression -> {accessor: expected value or RAISES}
#
# Read the two tables side by side: they disagree in 11 of 20 cells, which is
# the whole point. `"42"` is a string an int-accessor rejects but an
# int-coercer accepts; `42` is an int a string-accessor rejects but a
# string-coercer stringifies.
STRICT_TABLE: dict[str, dict[str, Any]] = {
    '"42"': {"as_int": RAISES, "as_float": RAISES, "as_bool": RAISES, "as_string": "42"},
    # forceFloat accepts an int, so as_float widens 42 -> 42.0; forceInt does
    # not narrow, so as_int rejects a float even when it is integral.
    "42": {"as_int": 42, "as_float": 42.0, "as_bool": RAISES, "as_string": RAISES},
    "42.0": {"as_int": RAISES, "as_float": 42.0, "as_bool": RAISES, "as_string": RAISES},
    "true": {"as_int": RAISES, "as_float": RAISES, "as_bool": True, "as_string": RAISES},
    # No leniency anywhere in this family, including null -> bool. Use
    # is_null()/coerce_str() when a null should mean something other than an
    # error.
    "null": {"as_int": RAISES, "as_float": RAISES, "as_bool": RAISES, "as_string": RAISES},
}

# `coerce_str` is `builtins.toString`, delegated to Nix on both engines rather
# than reimplemented in Python. These expectations were taken from a real
# `nix eval` of `builtins.toString` over the same expressions -- so if Nix ever
# changes them, this fails rather than silently diverging.
#
# The last four cases are the ones that matter: no plausible Python
# reimplementation joins lists on a space or honours `__toString`/`outPath`, so
# they are what distinguishes delegating from approximating.
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

# Nix has no string coercion for a bare attrset, and neither do we.
NIX_TOSTRING_REJECTS = ["{ a = 1; }", "x: x"]

# rpc-only, and NOT Nix operations -- Nix has no toInt/toFloat/toBool. They are
# kept as-is rather than ported to inproc; see TODO.md.
LENIENT_TABLE: dict[str, dict[str, Any]] = {
    '"42"': {"coerce_int": 42, "coerce_float": 42.0, "coerce_bool": RAISES},
    "42": {"coerce_int": 42, "coerce_float": 42.0, "coerce_bool": RAISES},
    "42.0": {"coerce_int": 42, "coerce_float": 42.0, "coerce_bool": RAISES},
    "true": {"coerce_int": RAISES, "coerce_float": RAISES, "coerce_bool": True},
    "null": {"coerce_int": RAISES, "coerce_float": RAISES, "coerce_bool": RAISES},
}


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


@pytest.mark.parametrize("expr", list(LENIENT_TABLE), ids=list(LENIENT_TABLE))
async def test_rpc_coerce_accessors_convert(expr: str, rpc_session: RpcSessionFactory) -> None:
    """``coerce_int``/``coerce_float``/``coerce_bool`` convert rather than reject."""
    async with rpc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(expr)
        for name, expected in LENIENT_TABLE[expr].items():
            await _check(getattr(value, name), expected, NixCoercionError, f"{expr}.{name}")


@pytest.mark.parametrize("expr", list(NIX_TOSTRING), ids=list(NIX_TOSTRING))
async def test_inproc_coerce_str_is_nix_tostring(expr: str, inproc_session: InprocSessionFactory) -> None:
    """inproc's ``coerce_str`` is ``builtins.toString``, not an approximation."""
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(expr)
        assert await value.coerce_str() == NIX_TOSTRING[expr]


@pytest.mark.parametrize("expr", list(NIX_TOSTRING), ids=list(NIX_TOSTRING))
async def test_rpc_coerce_str_is_nix_tostring(expr: str, rpc_session: RpcSessionFactory) -> None:
    """rpc's ``coerce_str`` agrees with inproc's, because both are Nix's own."""
    async with rpc_session() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string(expr)
        assert await value.coerce_str() == NIX_TOSTRING[expr]


@pytest.mark.parametrize("expr", NIX_TOSTRING_REJECTS, ids=NIX_TOSTRING_REJECTS)
async def test_coerce_str_rejects_what_nix_rejects(
    expr: str,
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """A value Nix will not stringify is an error on both engines, not a fallback."""
    async with inproc_session() as session, session.store() as store, session.eval(store) as ev:
        with pytest.raises(NixError):
            await (await ev.string(expr)).coerce_str()

    async with rpc_session() as session, session.store() as store, session.eval(store) as ev:
        with pytest.raises(NixError):
            await (await ev.string(expr)).coerce_str()


# The strict accessor's real cross-engine counterpart, by NixType rather than
# by four method names. Bool is omitted: inproc's as_bool(null) -> False has no
# rpc equivalent, which is itself part of the open decision.
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
