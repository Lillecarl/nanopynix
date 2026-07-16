"""Thread-confined asynchronous execution for Nix C++ objects."""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")


class NixThreadExecutor:
    """Run all Nix operations serially on one dedicated thread.

    Nix has process-global mutable state and its store/evaluator objects are
    thread-affine. This executor is shared by the RPC worker and the
    in-process API, while allowing their Python callers to remain asynchronous.
    """

    def __init__(self) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="nix")

    async def run(self, func: Callable[..., _T], *args: Any) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, func, *args)

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)
