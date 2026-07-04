"""Nix manager — lifecycle, configuration, and entry point for all facades.

Manages subprocess workers via a ``WorkerPool``.  Each worker is an independent
Nix process (forkserver-based) with its own Store connection, logger, and
configuration.

Usage::

    async with Nix(max_workers=4, store_uri="daemon",
                    experimental_features=["flakes"]) as nix:
        info = await nix.store.query_path_info("/nix/store/...")
        async for event in nix.log_stream():
            ...
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from nanopynix._pool import WorkerPool
from nanopynix._session import EvalSession
from nanopynix.models import LogEvent
from nanopynix.store import Store


class Nix:
    """Nix runtime manager — subprocess worker pool."""

    def __init__(
        self,
        *,
        max_workers: int = 1,
        store_uri: str = "auto",
        eval_store_uri: str | None = None,
        settings: dict[str, str] | None = None,
        experimental_features: list[str] | None = None,
        rpc_timeout: float = 300.0,
        idle_timeout: float | None = None,
    ) -> None:
        self._pool = WorkerPool(
            max_workers=max_workers,
            store_uri=store_uri,
            eval_store_uri=eval_store_uri,
            settings=settings,
            experimental_features=experimental_features,
            rpc_timeout=rpc_timeout,
            idle_timeout=idle_timeout,
        )
        self.store: Store | None = None

    async def open(self) -> None:
        """Spawn workers and initialize the store facade."""
        await self._pool.open()
        self.store = Store(self._pool)

    async def close(self) -> None:
        """Shut down all workers."""
        try:
            async with asyncio.timeout(60):
                await self._pool.close()
        except TimeoutError:
            import sys
            print("nanopynix: timed out closing worker pool", file=sys.stderr)

    async def __aenter__(self) -> Nix:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def log_stream(self) -> AsyncIterator[LogEvent]:
        """Async iterator over log events from all workers.

        Each event is a validated ``LogEvent`` model.
        """
        async for raw in self._pool.log_stream():
            if raw is None:
                continue  # worker close sentinel
            # Wire format uses "id"; model uses "request_id"
            # result events carry a ResultType int in args[1]
            data: dict = {
                "request_id": raw["id"],
                "action": raw["action"],
                "args": raw["args"],
            }
            if raw["action"] == "result" and len(raw["args"]) > 1:
                data["result_type"] = raw["args"][1]
            yield LogEvent.model_validate(data)

    def eval(self) -> EvalSession:
        """Acquire a worker exclusively for an eval session.

        Usage::

            async with nix.eval() as session:
                root = await session.eval_file("/path/to/flake.nix")
                meta = await root.attr("meta")
                desc = await meta.force()

        Returns an ``EvalSession`` context manager that locks a single
        worker for the duration.  All exported handles are released on
        exit.  RPC calls use ``rpc_timeout`` from the ``Nix`` instance.
        """
        return EvalSession(self._pool, timeout=self._pool.rpc_timeout)
