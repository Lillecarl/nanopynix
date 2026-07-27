"""Conformance checks for transport-neutral public protocols.

Four checks, because a protocol can be wrong in more directions than the
obvious one.

``test_protocol_static_conformance`` is the classic one, run by pyright: does
each engine's concrete class satisfy the protocol it claims to? That catches a
class that drops or changes a declared member.

``test_protocols_declare_the_whole_shared_surface`` is the other direction:
does the protocol declare everything both engines actually share? Nothing
checked that until it was added, and the protocols had drifted badly as a
result -- ``AsyncValue`` declared 6 of 21 shared members, ``AsyncEvalSession``
6 of 8. An under-declared protocol passes conformance while promising almost
nothing, so the three ``Value`` divergences the ledger tracks were free to
persist: no gate was looking at them.

The remaining two are about runtime, and exist because beartype checks these
hints for real. ``test_every_protocol_is_runtime_checkable`` pins the
decorator that lets it do so at all, and
``test_both_engines_pass_the_runtime_conformance_check`` re-asks the
conformance question the way beartype will ask it. The *consequence* of
getting the first one wrong -- beartype silently skipping whole callables --
is asserted a file away, in
``test_beartype_instrumentation.py::test_no_callable_is_silently_undecoratable_beyond_the_known_list``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from nanopynix import inproc
from nanopynix.protocols import (
    AsyncEvalSession,
    AsyncLockedFlake,
    AsyncReplSession,
    AsyncStore,
    AsyncValue,
    AsyncVerbosityController,
)
from nanopynix.rpc.client import _session as rpc_private
from nanopynix.rpc.client.store import Store as RpcStoreImpl

if TYPE_CHECKING:
    from nanopynix.inproc import (
        EvalSession as InprocEvalSession,
        LockedFlake,
        ReplSession as InprocReplSession,
        Session as InprocSession,
        Store as InprocStore,
        Value,
    )
    from nanopynix.rpc.client._session import (
        EvalSession as RpcEvalSession,
        LockedFlakeHandle,
        ReplSession as RpcReplSession,
        ValueProxy,
    )
    from nanopynix.rpc.client.session import Session as RpcSession
    from nanopynix.rpc.client.store import Store as RpcStore


def _accept_async_value(value: AsyncValue) -> None:
    del value


def _accept_async_store(store: AsyncStore) -> None:
    del store


def _accept_async_locked_flake(locked_flake: AsyncLockedFlake) -> None:
    del locked_flake


def _accept_async_eval_session(eval_session: AsyncEvalSession) -> None:
    del eval_session


def _accept_async_repl_session[ReplValueT: AsyncValue](repl_session: AsyncReplSession[ReplValueT]) -> None:
    del repl_session


def _accept_async_verbosity_controller[VerbosityT](controller: AsyncVerbosityController[VerbosityT]) -> None:
    del controller


def test_protocol_static_conformance() -> None:
    """Keep structural compatibility checked by pyright without constructing Nix."""
    if TYPE_CHECKING:
        value_proxy = cast("ValueProxy", None)
        inproc_value = cast("Value", None)
        rpc_eval_session = cast("RpcEvalSession", None)
        inproc_eval_session = cast("InprocEvalSession", None)
        rpc_repl_session = cast("RpcReplSession", None)
        inproc_repl_session = cast("InprocReplSession", None)
        rpc_session = cast("RpcSession", None)
        inproc_session = cast("InprocSession", None)
        rpc_store = cast("RpcStore", None)
        inproc_store = cast("InprocStore", None)
        inproc_locked_flake = cast("LockedFlake", None)
        locked_flake = cast("LockedFlakeHandle", None)
        _accept_async_value(value_proxy)
        _accept_async_value(inproc_value)
        _accept_async_store(rpc_store)
        _accept_async_store(inproc_store)
        _accept_async_eval_session(rpc_eval_session)
        _accept_async_eval_session(inproc_eval_session)
        _accept_async_repl_session(rpc_repl_session)
        _accept_async_repl_session(inproc_repl_session)
        _accept_async_verbosity_controller(rpc_session)
        _accept_async_verbosity_controller(inproc_session)
        _accept_async_locked_flake(inproc_locked_flake)
        _accept_async_locked_flake(locked_flake)


# Members both engines have that the protocol deliberately does not declare,
# with why. Each must name a real reason -- "no signature is true of both
# engines" -- not "we did not get to it". Every one of these is also recorded
# in tests/nanopynix/test_engine_parity.py, which is where the work to remove
# it is tracked; this table only records that the protocol is knowingly silent.
UNDECLARED: dict[str, str] = {}

# (protocol name, protocol, inproc class, rpc class)
PROTOCOL_PAIRS: list[tuple[str, type, type, type]] = [
    ("AsyncValue", AsyncValue, inproc.Value, rpc_private.ValueProxy),
    ("AsyncStore", AsyncStore, inproc.Store, RpcStoreImpl),
    ("AsyncEvalSession", AsyncEvalSession, inproc.EvalSession, rpc_private.EvalSession),
    ("AsyncReplSession", AsyncReplSession, inproc.ReplSession, rpc_private.ReplSession),
    ("AsyncLockedFlake", AsyncLockedFlake, inproc.LockedFlake, rpc_private.LockedFlakeHandle),
]


def _public(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_")}


def test_every_protocol_is_runtime_checkable() -> None:
    """Because a protocol that is not silently disables beartype around it.

    beartype cannot decorate a function whose parameter is annotated with a
    protocol it cannot pass to ``isinstance``; it skips the whole function and
    warns. That failure is invisible from inside a passing test suite, so the
    property is pinned here rather than left to the warning nobody reads.
    """
    # `_is_runtime_protocol` is what `typing.runtime_checkable` sets and what
    # `_ProtocolMeta.__instancecheck__` reads; there is no public accessor.
    not_checkable = [
        name for name, protocol, _, _ in PROTOCOL_PAIRS if not getattr(protocol, "_is_runtime_protocol", False)
    ]
    assert not not_checkable, f"these protocols are not @runtime_checkable: {not_checkable}"


def _conforms_at_runtime(cls: type, protocol: type) -> bool:
    """Runtime conformance, by whichever check ``typing`` permits for ``protocol``.

    ``issubclass`` is the direct question and is what we ask when we can. But
    ``typing`` forbids it against a protocol with a non-method member --
    ``AsyncReplSession.line_editors`` is one -- and skipping those would leave
    the one protocol that needs a different path checked by nothing.

    ``isinstance`` answers the same question there, and it is also the check
    beartype itself performs, so it is if anything the more relevant of the
    two. It needs an instance rather than a class; ``object.__new__`` gives an
    uninitialized one, which is enough because
    ``_ProtocolMeta.__instancecheck__`` reads members with
    ``inspect.getattr_static`` -- looked up on the type, never invoked. That
    matters for the rpc class in particular: it is ``__slots__``-based, so
    every instance attribute is unset here and a getter-invoking check would
    see nothing. Both engines declare ``line_editors`` as a property on the
    class, so the static lookup finds it. No Nix state is constructed.
    """
    if all(callable(getattr(protocol, member, None)) for member in _public(protocol)):
        return issubclass(cls, protocol)
    return isinstance(object.__new__(cls), protocol)


def test_both_engines_pass_the_runtime_conformance_check() -> None:
    """The static conformance above, asserted again at runtime.

    Not redundant: pyright checks the classes named in ``PROTOCOL_PAIRS``,
    while this checks the objects beartype will actually see -- and beartype's
    check is structural at runtime, so a class can satisfy pyright and still
    fail here (a member supplied by a ``TYPE_CHECKING``-only declaration, say).
    """
    for name, protocol, inproc_cls, rpc_cls in PROTOCOL_PAIRS:
        assert _conforms_at_runtime(inproc_cls, protocol), f"inproc {inproc_cls.__name__} fails {name} at runtime"
        assert _conforms_at_runtime(rpc_cls, protocol), f"rpc {rpc_cls.__name__} fails {name} at runtime"


def test_protocols_declare_the_whole_shared_surface() -> None:
    """Every member both engines share must be declared, or excused in UNDECLARED.

    This is the gate that keeps a protocol from quietly becoming decorative.
    Conformance alone cannot: a protocol declaring nothing at all passes it.
    """
    undeclared: dict[str, str] = {}
    for name, protocol, inproc_cls, rpc_cls in PROTOCOL_PAIRS:
        shared = _public(inproc_cls) & _public(rpc_cls)
        for member in sorted(shared - _public(protocol)):
            key = f"{name}.{member}"
            undeclared[key] = UNDECLARED.get(key, "UNJUSTIFIED")

    unjustified = sorted(key for key, reason in undeclared.items() if reason == "UNJUSTIFIED")
    assert not unjustified, (
        "both engines have these members but the protocol does not declare them -- "
        f"declare them, or add them to UNDECLARED with a reason: {unjustified}"
    )

    stale = sorted(set(UNDECLARED) - set(undeclared))
    assert not stale, f"UNDECLARED lists members the protocol now declares (or that no longer exist): {stale}"
