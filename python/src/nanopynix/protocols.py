"""Transport-neutral public protocols for nanopynix's asynchronous APIs.

The RPC and in-process implementations have different construction and
lifetime mechanics, but callers can depend on these shared operation shapes.
Protocols deliberately describe only behaviour common to both transports.
"""

from __future__ import annotations

from typing import Any, Protocol, Self


class AsyncValue(Protocol):
    """The common asynchronous value lifecycle and forcing interface."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def force(self) -> Any: ...

    async def force_deep(self) -> Any: ...

    async def force_json(self, *, copy_to_store: bool = False) -> Any: ...

    async def realise_string(self) -> str: ...

    async def realise_argv(self) -> list[str]: ...

    async def edit_location(self) -> tuple[str, int]: ...

    async def release(self) -> None: ...


class AsyncLockedFlake(Protocol):
    """The common lifecycle for an in-memory flake lock."""

    async def eval(self) -> AsyncValue: ...

    async def write_lock_file(self) -> None: ...

    async def release(self) -> None: ...


__all__ = ["AsyncLockedFlake", "AsyncValue"]
