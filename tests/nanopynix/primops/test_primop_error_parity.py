"""A raising primop reads the same wherever it ran.

Three paths reach the Nix evaluator, and a caller should not be able to tell
them apart from the error:

* ``import_path`` on the inproc engine -- the function runs in this process;
* ``import_path`` on the rpc engine -- the worker imports and runs it;
* ``rpc=True`` on the rpc engine -- the function runs on the **client**, and
  its failure crosses the backchannel to the worker.

The third one did not match. Measured before this file existed, a primop
raising ``PrimopError("no such user")`` reached the caller as::

    error: RemoteCallError: (<Status.INTERNAL: 13>, 'no such user', None)

against ``error: no such user`` from the other two. The cause was a double
stringification and not a lost class: the backchannel frame carries one
``error: str`` field, so the ``GRPCError`` the manager handler raised had its
status discarded and its ``repr`` used as the message.
:mod:`nanopynix.rpc._primop_wire` explains the repair.

**This file compares the rendered text, and that is deliberate.**
``tests/nanopynix/test_engine_parity_semantics.py`` compares exception types
and says why it does not compare messages. It cannot cover this: all three
paths already produce ``EvalError``, on both engines, before and after the
repair. Nix wraps whatever a primop raises into its own evaluation failure, so
the type was never the thing that differed and the type can never be the thing
that agrees. What a caller actually reads is the message.

**No caller gets ``PrimopError`` as a class, and that is the design.** #33 asks
for one that "names ``PrimopError``". It should not: the class exists to make
Nix show a message *bare*, with no type-name prefix, and
``test_primop_error.py`` has always asserted ``"PrimopError" not in message``.
Naming it in the manager path would have made that path the odd one out again,
in the other direction. The target is what inproc does, and inproc shows the
message bare.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from nanopynix import PrimopError, strip_ansi
from nanopynix.models import PrimOpSpec
from nanopynix.rpc._primop_wire import MARKER, decode, encode

if TYPE_CHECKING:
    from tests.support.nix_environment import NixTestEnvironment

pytestmark = pytest.mark.nix_version(minimum="2.32")

_HERE = "tests.nanopynix.primops.test_primop_error_parity"


class CallerOwnError(RuntimeError):
    """A class nanopynix has never seen, from the caller's own code."""


def raise_primop_error(_value: object) -> object:
    raise PrimopError("deliberate rejection, shown bare")


def raise_caller_class(_value: object) -> object:
    raise CallerOwnError("unexpected failure")


def raise_value_error(_value: object) -> object:
    raise ValueError("also deliberate, for backward compatibility")


def _echo(value: object) -> object:
    """A primop that works, to show the bridge still answers after a failure."""
    return value


_CALLABLES = {
    "parityPrimopError": raise_primop_error,
    "parityCallerClass": raise_caller_class,
    "parityValueError": raise_value_error,
}


def _specs(*, rpc: bool) -> list[PrimOpSpec]:
    common: dict[str, Any] = {"arity": 1, "args": ["value"], "doc": "raises, for a parity test"}
    if rpc:
        return [PrimOpSpec(name=name, rpc=True, **common) for name in _CALLABLES]
    return [
        PrimOpSpec(name=name, import_path=f"{_HERE}:{func.__name__}", **common) for name, func in _CALLABLES.items()
    ]


def _primop_line(exc: BaseException) -> str:
    """The last ``error:`` line, which is what the primop contributed.

    Nix prefixes its own "while calling the builtin" frame and colourises the
    whole thing, and neither is what this file is about.
    """
    return strip_ansi(str(exc)).rsplit("error:", 1)[-1].strip()


async def _render(session: Any, name: str) -> str:
    async with session as opened, opened.store() as store, opened.eval(store) as evaluator:
        with pytest.raises(Exception) as caught:  # noqa: PT011 -- the message is the assertion; the type is EvalError on every path and is pinned below
            await (await evaluator.string(f"builtins.{name} null")).to_python()
        assert type(caught.value).__name__ == "EvalError", "Nix wraps a primop failure; that is the shared type"
        return _primop_line(caught.value)


@pytest.mark.parametrize(
    ("primop", "expected"),
    [
        ("parityPrimopError", "deliberate rejection, shown bare"),
        ("parityValueError", "also deliberate, for backward compatibility"),
        ("parityCallerClass", "CallerOwnError: unexpected failure"),
    ],
)
async def test_all_three_primop_paths_render_the_same(
    shared_nix_environment: NixTestEnvironment, primop: str, expected: str
) -> None:
    """The parity assertion, and the reason this file exists.

    ``PrimopError`` and ``ValueError`` are the two classes the C++ bridge
    treats as a deliberate rejection, so their messages are shown bare. Any
    other class keeps its name as a prefix, which is the signal that the
    primop failed rather than rejected.
    """
    inproc = await _render(shared_nix_environment.inproc_session(primops=_specs(rpc=False)), primop)
    rpc_imported = await _render(shared_nix_environment.rpc_session(primops=_specs(rpc=False)), primop)
    rpc_manager = await _render(
        shared_nix_environment.rpc_session(primops=_specs(rpc=True), primop_callables=_CALLABLES),
        primop,
    )

    assert inproc == expected
    assert rpc_imported == expected, "the worker-imported path drifted from inproc"
    assert rpc_manager == expected, "the manager path drifted; see nanopynix.rpc._primop_wire"


async def test_a_manager_primop_failure_leaves_nothing_behind(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """A failed manager primop does not poison the session or the bridge.

    #33 asked for this because one candidate repair was a side table keyed by
    request, which would leak when an evaluation was cancelled. The repair
    that landed keeps no state at all -- the whole answer travels in the one
    string the frame carries -- so there is nothing to leak. This pins that:
    the same evaluator keeps working, and a later good call still returns.
    """
    specs = [*_specs(rpc=True), PrimOpSpec(name="parityEcho", arity=1, args=["value"], doc="echo", rpc=True)]
    callables = {**_CALLABLES, "parityEcho": _echo}
    async with (
        shared_nix_environment.rpc_session(primops=specs, primop_callables=callables) as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        for _ in range(3):
            with pytest.raises(Exception):  # noqa: PT011, B017 -- the failure is the setup, not the assertion
                await (await evaluator.string("builtins.parityCallerClass null")).to_python()
        assert await (await evaluator.string("builtins.parityEcho 42")).as_int() == 42


def test_encode_mirrors_the_cpp_rule() -> None:
    """``encode`` and ``py_primop_bridge`` must agree on what is deliberate.

    A class treated as deliberate in one and unexpected in the other would put
    the prefix on for one path and not the other, which is the divergence this
    whole file exists to prevent.
    """
    assert decode(encode(PrimopError("bare"))) == "bare"
    assert decode(encode(ValueError("bare too"))) == "bare too"
    assert decode(encode(CallerOwnError("prefixed"))) == "CallerOwnError: prefixed"
    assert decode(encode(KeyError("k"))) == "KeyError: 'k'"


def test_decode_leaves_an_unmarked_message_alone() -> None:
    """A dead backchannel must not be dressed up as the caller's exception.

    Both arrive at the worker as the same ``RemoteCallError``. Only the marker
    tells them apart, so the negative direction is the one worth pinning.
    """
    assert decode("connection reset") is None
    assert decode("") is None
    assert decode(MARKER) == ""
