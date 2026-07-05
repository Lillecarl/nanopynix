"""Session manager — lifecycle, configuration, and entry point for all facades.

Manages a single subprocess worker via ``_WorkerManager``.  The worker is an
independent Nix process (forkserver-based) with its own Store connection,
logger, and configuration.

Usage::

    async with Session(store_uri="daemon",
                       experimental_features=["flakes"]) as session:
        info = await session.store.query_path_info("/nix/store/...")
        async for event in session.log_stream():
            ...
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from nanopynix._pool import _WorkerManager
from nanopynix._session import EvalSession
from nanopynix.models import LogEvent
from nanopynix.store import Store

logger = logging.getLogger(__name__)


class Session:
    """Session runtime — manages a single subprocess worker.

    Usage::

        async with Session(
            store_uri="daemon",
            settings={"max-jobs": "4"},
            experimental_features=["flakes"],
        ) as session:
            async with session.store() as store:
                info = await store.query_path_info(sp)
    """

    def __init__(
        self,
        *,
        store_uri: str = "auto",
        eval_store_uri: str | None = None,
        settings: dict[str, str] | None = None,
        experimental_features: list[str] | None = None,
    ) -> None:
        self._manager = _WorkerManager(
            store_uri=store_uri,
            eval_store_uri=eval_store_uri,
            settings=settings,
            experimental_features=experimental_features,
        )
        self.store: Store | None = None

    async def open(self) -> None:
        """Spawn worker and initialize the store facade."""
        await self._manager.open()
        self.store = Store(self._manager)

    async def close(self) -> None:
        """Shut down the worker."""
        try:
            async with asyncio.timeout(60):
                await self._manager.close()
        except TimeoutError:
            logger.warning("nanopynix: timed out closing worker")

    async def __aenter__(self) -> Session:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def log_stream(self) -> AsyncIterator[LogEvent]:
        """Async iterator over log events from the worker.

        Each event is a validated ``LogEvent`` model.
        """
        async for raw in self._manager.log_stream():
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
        """Acquire the worker exclusively for an eval session.

        Usage::

            async with session.eval() as eval_:
                root = await eval_.eval_file("/path/to/flake.nix")
                meta = await root.attr("meta")
                desc = await meta.force()

        Returns an ``EvalSession`` context manager that holds the worker
        for the duration.  All exported handles are released on exit.
        """
        return EvalSession(self._manager)


# Backward-compatible alias
Nix = Session
