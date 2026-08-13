"""The ``as_*`` value accessors must reject wrong types, not read the union.

``nix::Value``'s ``integer()``/``fpoint()``/``boolean()``/``c_str()`` are
unchecked reads of the value's storage union (``nix/expr/value.hh``). Calling
one on a value of another type is undefined behaviour: ``as_string()`` on an
int **segfaulted the interpreter**, and the numeric accessors returned garbage
just as silently.

These are the cheapest possible guard on that. A crash here takes the whole
process down, so a regression would otherwise surface as an unexplained
worker death rather than a failing assertion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import nanopynix

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import InprocSessionFactory

# (expression, the accessors that must raise for it)
WRONG_TYPE_CASES = [
    ("42", ("as_string", "as_bool")),
    ("42.5", ("as_string", "as_bool", "as_int")),
    ('"text"', ("as_int", "as_float", "as_bool")),
    ("true", ("as_string", "as_int", "as_float")),
    ("{ a = 1; }", ("as_string", "as_int", "as_float", "as_bool")),
    ("[ 1 2 ]", ("as_string", "as_int", "as_float", "as_bool")),
    ("x: x", ("as_string", "as_int", "as_float", "as_bool")),
    # null included deliberately: as_bool used to read it as False.
    ("null", ("as_string", "as_int", "as_float", "as_bool")),
]


@pytest.mark.parametrize(("expr", "accessors"), WRONG_TYPE_CASES, ids=[case[0] for case in WRONG_TYPE_CASES])
async def test_accessor_on_the_wrong_type_raises_instead_of_crashing(
    expr: str,
    accessors: tuple[str, ...],
    inproc_session: InprocSessionFactory,
) -> None:
    async with inproc_session() as session, session.store() as store, session.eval(store) as eval_session:
        value = await eval_session.string(expr)
        for accessor in accessors:
            with pytest.raises(nanopynix.NixError):
                await getattr(value, accessor)()


@pytest.mark.parametrize(
    ("expr", "accessor", "expected"),
    [
        ("42", "as_int", 42),
        ("42.5", "as_float", 42.5),
        ('"text"', "as_string", "text"),
        ("true", "as_bool", True),
        ("false", "as_bool", False),
    ],
    ids=["int", "float", "string", "true", "false"],
)
async def test_accessor_on_the_right_type_still_works(
    expr: str,
    accessor: str,
    expected: object,
    inproc_session: InprocSessionFactory,
) -> None:
    async with inproc_session() as session, session.store() as store, session.eval(store) as eval_session:
        value = await eval_session.string(expr)
        assert await getattr(value, accessor)() == expected


async def test_accessors_force_a_thunk_first(inproc_session: InprocSessionFactory) -> None:
    """The raw union accessors were unsafe on unforced values too.

    Nothing here forces before reading, so if the accessor did not force
    internally it would be reading a thunk's storage as an int.
    """
    async with inproc_session() as session, session.store() as store, session.eval(store) as eval_session:
        value = await eval_session.string("let x = 1 + 1; in x")
        assert await value.as_int() == 2


async def test_an_int_read_as_a_string_names_both_types(inproc_session: InprocSessionFactory) -> None:
    """The failure has to say what was wrong, not just that something was.

    This is the exact call that used to segfault.
    """
    async with inproc_session() as session, session.store() as store, session.eval(store) as eval_session:
        value = await eval_session.string("42")
        with pytest.raises(nanopynix.NixError) as excinfo:
            await value.as_string()
        message = str(excinfo.value).lower()
        assert "string" in message, message
        assert "int" in message, message
