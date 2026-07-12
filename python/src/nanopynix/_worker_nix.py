"""Dedicated Nix evaluation thread — keeps the asyncio event loop free.

All Nix C++ operations run on this single thread via a
``ThreadPoolExecutor(max_workers=1)``.  The event loop never blocks
on Nix evaluation, so log relaying and primop RPC dispatch stay live.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")


class NixThreadExecutor:
    """Serial executor for all Nix C++ work.

    Usage from an asyncio handler::

        result = await executor.run(self._do_eval_string, message)
    """

    def __init__(self) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="nix"
        )

    async def run(self, func: Callable[..., _T], *args: Any) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, func, *args)

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)
