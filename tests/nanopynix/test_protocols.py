"""Static conformance checks for transport-neutral public protocols."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from nanopynix.protocols import AsyncLockedFlake, AsyncValue

if TYPE_CHECKING:
    from nanopynix._session import LockedFlakeHandle, ValueProxy
    from nanopynix.inproc import Value


def _accept_async_value(value: AsyncValue) -> None:
    del value


def _accept_async_locked_flake(locked_flake: AsyncLockedFlake) -> None:
    del locked_flake


def test_protocol_static_conformance() -> None:
    """Keep structural compatibility checked by pyright without constructing Nix."""
    if TYPE_CHECKING:
        value_proxy = cast(ValueProxy, None)
        inproc_value = cast(Value, None)
        locked_flake = cast(LockedFlakeHandle, None)
        _accept_async_value(value_proxy)
        _accept_async_value(inproc_value)
        _accept_async_locked_flake(locked_flake)
