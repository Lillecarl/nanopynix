"""Static conformance checks for transport-neutral public protocols."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar, cast

from nanopynix.protocols import (
    AsyncEvalSession,
    AsyncLockedFlake,
    AsyncReplSession,
    AsyncStore,
    AsyncValue,
    AsyncVerbosityController,
)

ReplValueT = TypeVar("ReplValueT", bound=AsyncValue)
VerbosityT = TypeVar("VerbosityT")

if TYPE_CHECKING:
    from nanopynix._session import EvalSession as RpcEvalSession
    from nanopynix._session import LockedFlakeHandle, ValueProxy
    from nanopynix._session import ReplSession as RpcReplSession
    from nanopynix.inproc import EvalSession as InprocEvalSession
    from nanopynix.inproc import LockedFlake, Value
    from nanopynix.inproc import ReplSession as InprocReplSession
    from nanopynix.inproc import Session as InprocSession
    from nanopynix.inproc import Store as InprocStore
    from nanopynix.nix import Session as RpcSession
    from nanopynix.store import Store as RpcStore


def _accept_async_value(value: AsyncValue) -> None:
    del value


def _accept_async_store(store: AsyncStore) -> None:
    del store


def _accept_async_locked_flake(locked_flake: AsyncLockedFlake) -> None:
    del locked_flake


def _accept_async_eval_session(eval_session: AsyncEvalSession) -> None:
    del eval_session


def _accept_async_repl_session(repl_session: AsyncReplSession[ReplValueT]) -> None:
    del repl_session


def _accept_async_verbosity_controller(controller: AsyncVerbosityController[VerbosityT]) -> None:
    del controller


def test_protocol_static_conformance() -> None:
    """Keep structural compatibility checked by pyright without constructing Nix."""
    if TYPE_CHECKING:
        value_proxy = cast(ValueProxy, None)
        inproc_value = cast(Value, None)
        rpc_eval_session = cast(RpcEvalSession, None)
        inproc_eval_session = cast(InprocEvalSession, None)
        rpc_repl_session = cast(RpcReplSession, None)
        inproc_repl_session = cast(InprocReplSession, None)
        rpc_session = cast(RpcSession, None)
        inproc_session = cast(InprocSession, None)
        rpc_store = cast(RpcStore, None)
        inproc_store = cast(InprocStore, None)
        inproc_locked_flake = cast(LockedFlake, None)
        locked_flake = cast(LockedFlakeHandle, None)
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
