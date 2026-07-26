"""Conformance checks for transport-neutral public protocols.

Two checks, because a protocol can be wrong in two directions.

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
    from nanopynix.inproc import EvalSession as InprocEvalSession
    from nanopynix.inproc import LockedFlake, Value
    from nanopynix.inproc import ReplSession as InprocReplSession
    from nanopynix.inproc import Session as InprocSession
    from nanopynix.inproc import Store as InprocStore
    from nanopynix.rpc.client._session import EvalSession as RpcEvalSession
    from nanopynix.rpc.client._session import LockedFlakeHandle, ValueProxy
    from nanopynix.rpc.client._session import ReplSession as RpcReplSession
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
UNDECLARED: dict[str, str] = {
}

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
