"""Thread-safe bridge from Nix's C++ logger to Python consumers.

The C++ ``PyLogger`` calls the callback from *any* thread after
``gil_scoped_acquire``, so the callback must be thread-safe.  We use a
``janus.Queue`` which provides both a synchronous (thread-safe) interface
for the worker subprocess and an async interface for the Nix manager client.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import janus

if TYPE_CHECKING:
    from collections.abc import Callable

    type LogCallback = Callable[[int, str, object, ...], None]


class LogCollector:
    """Thread-safe collector for Nix log events.

    Pass ``collector.callback`` to ``nanopynix_util.install_logger()``.

    Usage::

        collector = LogCollector()
        nanopynix_util.install_logger(collector.callback)

        # Sync drain (worker subprocess):
        for event in collector.drain():
            ...

        # Async stream (Nix manager client):
        async for event in collector.stream():
            ...

        collector.stop()
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._queue = janus.Queue(maxsize=maxsize)

    # ── callback (thread-safe, called from C++ on any GIL thread) ──

    def callback(self, req_id: int, action: str, *args: object) -> None:
        """Push an event onto the queue — thread-safe."""
        self._queue.sync_q.put_nowait((req_id, action, *args))

    # ── sync drain (for the worker subprocess) ─────────────────────

    def drain(self) -> list:
        """Return all currently pending events without blocking."""
        events: list = []
        try:
            while True:
                events.append(self._queue.sync_q.get_nowait())
        except janus.SyncQueueEmpty:
            pass
        return events

    # ── async stream (for the Nix manager client) ──────────────────

    async def stream(self) -> AsyncIterator:
        """Yield events as they arrive.

        Terminates either on a ``None`` sentinel or when the queue is
        closed via ``close()`` / ``aclose()``.
        """
        try:
            while True:
                item = await self._queue.async_q.get()
                if item is None:
                    break
                yield item
        except asyncio.queues.QueueShutDown:
            pass

    # ── shutdown ───────────────────────────────────────────────────

    def close(self) -> None:
        """Synchronous close — for the worker subprocess (no event loop)."""
        self._queue.close()

    async def aclose(self) -> None:
        """Async close — proper cleanup of janus internal tasks."""
        await self._queue.aclose()

    def send_sentinel(self) -> None:
        """Push a ``None`` sentinel to unblock ``stream()`` without closing."""
        self._queue.sync_q.put_nowait(None)
