"""Session manager — lifecycle, configuration, and entry point for all facades.

Manages a single subprocess worker via ``_WorkerManager``.  The worker is an
independent Nix process (forkserver-based) with its own Store connection,
logger, and configuration.

Usage::

    async with Session(store_uri="daemon", experimental_features=["flakes"]) as session:
        async with session.store() as store:
            info = await store.query_path_info("/nix/store/...")
        async for event in session.log_stream():
            ...
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from nanopynix._pool import _WorkerManager
from nanopynix._session import EvalSession
from nanopynix.models import LogEvent, PrimOpSpec
from nanopynix.store import StoreHandle

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

logger = logging.getLogger(__name__)


def _raw_log_event(raw: dict[str, Any]) -> LogEvent:
    data: dict[str, Any] = {
        "request_id": raw.get("request_id", raw.get("id", 0)),
        "action": raw["action"],
        "args": raw["args"],
    }
    if raw["action"] == "result" and len(raw["args"]) > 1:
        data["result_type"] = raw["args"][1]
    return LogEvent.model_validate(data)


class LogCapture:
    """Async context manager that records log events while active."""

    def __init__(self, manager: _WorkerManager) -> None:
        self._manager = manager
        self._sub = None
        self.events: list[LogEvent] = []

    async def __aenter__(self) -> LogCapture:
        def _append(raw: object) -> None:
            if raw is None:
                return
            if isinstance(raw, dict):
                self.events.append(_raw_log_event(raw))

        self._sub = self._manager.subscribe(_append)
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._sub is not None:
            self._sub.unsubscribe()
            self._sub = None


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
        nix_conf: str | None = "/etc/nix/nix.conf",
        config: dict[str, str] | None = None,
        settings: dict[str, str] | None = None,
        experimental_features: list[str] | None = None,
        primops: Sequence[PrimOpSpec | Mapping[str, Any]] | None = None,
        worker_oom_score_adj: int | None = 500,
        reserved_worker_oom_score_adj: int | None = 250,
    ) -> None:
        if config is not None and settings is not None:
            raise TypeError("Use either config= or settings=, not both")
        self._manager = _WorkerManager(
            store_uri=store_uri,
            eval_store_uri=eval_store_uri,
            nix_conf=nix_conf,
            settings=config if config is not None else settings,
            experimental_features=experimental_features,
            primops=[PrimOpSpec.model_validate(spec) for spec in primops or []],
            worker_oom_score_adj=worker_oom_score_adj,
            reserved_worker_oom_score_adj=reserved_worker_oom_score_adj,
        )
        self._session_id = uuid.uuid4().hex

    def store(self, uri: str = "daemon") -> StoreHandle:
        """Create a StoreHandle for store operations.

        Usage::

            async with session.store() as store:
                info = await store.query_path_info(sp)
                drv = await store.build_derivation(sp, mode)

        The handle carries this session's ID — passing it to
        ``Eval`` from a different session raises ``ValueError``.
        """
        return StoreHandle(self._manager, uri, self._session_id)

    async def open(self) -> None:
        """Spawn the worker subprocess."""
        await self._manager.open()

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
            yield _raw_log_event(raw)

    def capture_logs(self) -> LogCapture:
        """Record typed log events during an async context block."""
        return LogCapture(self._manager)

    def subscribe(self, callback):
        """Subscribe a callback to live log events.

        The callback receives raw event dicts from the worker
        (``{\"id\": ..., \"action\": ..., \"args\": [...]}``).
        Returns a handle — call ``.unsubscribe()`` to stop.

        Usage::

            sub = session.subscribe(lambda e: print(e[\"action\"]))
            ...
            sub.unsubscribe()
        """
        return self._manager.subscribe(callback)

    def eval(self, store: StoreHandle | None = None) -> EvalSession:
        """Acquire the worker exclusively for an eval session.

        Usage::

            async with session.store() as store:
                async with session.eval(store) as eval_:
                    root = await eval_.file("/path/to/flake.nix")

        Args:
            store: Optional StoreHandle for cross-session validation.
                   If provided and from a different session, raises ValueError.

        Returns an ``EvalSession`` context manager that holds the worker
        for the duration.  All exported handles are released on exit.
        """
        if store is not None and store._session_id != self._session_id:
            raise ValueError("StoreHandle belongs to a different session")
        return EvalSession(self._manager)


# Backward-compatible alias
Nix = Session
