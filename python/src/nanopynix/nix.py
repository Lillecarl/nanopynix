"""Session manager — lifecycle, configuration, and entry point for all facades.

Manages a single subprocess worker via ``_WorkerClient``.  The worker is an
independent Nix process (forkserver-based gRPC) with its own Store connection,
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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanopynix_proto.nix.common import LogEvent as LogEventProto
from nanopynix_proto.nix.common import LogLevel

import nanopynix_expr
from nanopynix._pool import _WorkerClient  # type: ignore[reportPrivateUsage] -- internal lifecycle integration
from nanopynix._process_title import set_manager_title
from nanopynix._session import EvalSession, ReplSession
from nanopynix.exceptions import EvalStateBusyError
from nanopynix.models import LogEvent, PrimOpSpec
from nanopynix.settings import NanopynixSettings, NixSettings, normalize_nix_settings
from nanopynix.store import Store, StoreHandle
from nanopynix.verbosity import LogLevelInput, normalize_log_level

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence
    from os import PathLike

    from nanopynix._pool import (
        _Subscription,  # type: ignore[reportPrivateUsage] -- type of _WorkerClient.subscribe result
    )

logger = logging.getLogger(__name__)


def _raw_log_event(raw: LogEventProto) -> LogEvent:
    """Convert a proto LogEvent to a pydantic LogEvent model."""
    return LogEvent.from_proto(raw)


class LogCapture:
    """Async context manager that records log events while active."""

    def __init__(self, manager: _WorkerClient) -> None:
        self._manager = manager
        self._sub: _Subscription | None = None
        self.events: list[LogEvent] = []

    async def __aenter__(self) -> LogCapture:
        def _append(raw: object) -> None:
            if raw is None:
                return
            if isinstance(raw, LogEventProto):
                self.events.append(_raw_log_event(raw))

        self._sub = self._manager.subscribe(_append)
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._sub is not None:
            self._sub.unsubscribe()
            self._sub = None


def _to_primop_specs(specs: Sequence[PrimOpSpec | Mapping[str, Any]] | None) -> list[PrimOpSpec]:
    """Convert a sequence of PrimOpSpecs or dicts to a list of PrimOpSpecs."""
    result: list[PrimOpSpec] = []
    for spec in specs or []:
        if isinstance(spec, PrimOpSpec):
            result.append(spec)
        else:
            result.append(PrimOpSpec(**spec))
    return result


def _normalize_nix_path(nix_path: str | Sequence[str] | None) -> list[str]:
    if nix_path is None:
        return list(nanopynix_expr.parse_nix_path())
    if isinstance(nix_path, str):
        return list(nanopynix_expr.parse_nix_path(nix_path))
    return list(nix_path)


class Session:
    """Session runtime — manages a single subprocess worker.

    Usage::

        async with Session(
            store_uri="daemon",
            settings=NixSettings(max_jobs=4),
        ) as session:
            async with session.store() as store:
                info = await store.query_path_info(str(sp))
    """

    def __init__(
        self,
        *,
        store_uri: str = "auto",
        nix_conf: Path | None = None,
        load_config: bool = True,
        settings: NixSettings | PathLike[str] | str | None = None,
        experimental_features: list[str] | None = None,
        verbosity: LogLevelInput | None = None,
        nix_path: str | Sequence[str] | None = None,
        primops: Sequence[PrimOpSpec | Mapping[str, Any]] | None = None,
        primop_callables: Mapping[str, Callable[..., Any]] | None = None,
        pure_eval: bool | None = None,
        restrict_eval: bool | None = None,
        allowed_uris: Sequence[str] | None = None,
        worker_oom_score_adj: int | None = None,
        runtime_settings: NanopynixSettings | None = None,
    ) -> None:
        set_manager_title()
        if nix_conf is not None:
            if not isinstance(nix_conf, Path):  # type: ignore[reportUnnecessaryIsInstance] -- runtime guard for untyped callers
                raise TypeError("nix_conf must be a pathlib.Path or None")
            if not nix_conf.exists():
                raise FileNotFoundError(nix_conf)
        nix_settings = normalize_nix_settings(settings).with_experimental_features(experimental_features)
        nanopynix_settings = runtime_settings or NanopynixSettings()
        self.runtime_settings = nanopynix_settings
        worker_settings = nix_settings.to_worker_settings()
        self._manager = _WorkerClient(
            store_uri=store_uri,
            nix_conf=nix_conf,
            load_config=load_config,
            settings=worker_settings,
            experimental_features=[],
            verbosity=normalize_log_level(verbosity) if verbosity is not None else None,
            nix_path=_normalize_nix_path(nix_path),
            primops=_to_primop_specs(primops),
            primop_callables=dict(primop_callables) if primop_callables is not None else None,
            pure_eval=pure_eval,
            restrict_eval=restrict_eval,
            allowed_uris=allowed_uris,
            worker_oom_score_adj=worker_oom_score_adj,
            rpc_timeout=nanopynix_settings.rpc_timeout,
            shutdown_timeout=nanopynix_settings.shutdown_timeout,
        )
        self._session_id = uuid.uuid4().hex
        self._active_eval: EvalSession | None = None

    def store(self, uri: str = "auto") -> Store:
        """Create an ergonomic Store facade for store operations.

        Usage::

            async with session.store() as store:
                info = await store.query_path_info(str(sp))
                drv = await store.rpc.build_derivation(
                    BuildDerivationRequest(path=str(sp), build_mode=mode)
                )

        The store carries this session's ID — passing it to
        ``Eval`` from a different session raises ``ValueError``. Opening the
        handle adds its URI to the worker's process title.
        """
        return Store(
            StoreHandle(
                self._manager,
                uri,
                self._session_id,
                self._manager.rpc_timeout,
            )
        )

    async def open(self) -> None:
        """Spawn the worker subprocess."""
        await self._manager.open()

    async def close(self) -> None:
        """Shut down the worker."""
        try:
            async with asyncio.timeout(60):
                active_eval = self._active_eval
                if active_eval is not None:
                    await active_eval.close()
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
            if not isinstance(raw, LogEventProto):
                logger.warning("nanopynix: ignored unexpected log event %r", raw)
                continue
            yield _raw_log_event(raw)

    def capture_logs(self) -> LogCapture:
        """Record typed log events during an async context block."""
        return LogCapture(self._manager)

    async def get_verbosity(self) -> LogLevel:
        """Return the worker-side Nix log verbosity."""
        return await self._manager.get_verbosity()

    async def set_verbosity(self, verbosity: LogLevelInput) -> LogLevel:
        """Set the worker-side Nix log verbosity for this session."""
        return await self._manager.set_verbosity(normalize_log_level(verbosity))

    def subscribe(self, callback: Any) -> Any:
        """Subscribe a callback to live log events.

        The callback receives raw ``LogEvent`` proto messages from the worker.
        Returns a handle — call ``.unsubscribe()`` to stop.

        Usage::

            sub = session.subscribe(lambda e: print(e.action))
            ...
            sub.unsubscribe()
        """
        return self._manager.subscribe(callback)

    def eval(self, store: Store) -> EvalSession:
        """Create this session's one permitted eval-session context manager.

        Usage::

            async with session.store() as store:
                async with session.eval(store) as eval_:
                    root = await eval_.file("/path/to/flake.nix")

        Args:
            store: Open Store to use for this eval state. If it belongs to
                   a different session, raises ValueError.

        Returns an ``EvalSession`` context manager. All exported handles are
        released on exit. Only one EvalSession or ReplSession may be open at a
        time; Store operations remain available while it is open.
        """
        if store._session_id != self._session_id:  # type: ignore[reportPrivateUsage] -- cross-session guard on internal ID
            raise ValueError("Store belongs to a different session")
        return EvalSession(
            self._manager,
            self,
            store.store_handle,
            session_id=self._session_id,
            rpc_timeout=self._manager.rpc_timeout,
            store=store,
        )

    def repl(self, store: Store) -> ReplSession:
        """Create this session's one permitted persistent Nix REPL.

        Bindings entered through :meth:`ReplSession.line` remain available
        until the returned context manager exits.
        """
        if store._session_id != self._session_id:  # type: ignore[reportPrivateUsage] -- cross-session guard on internal ID
            raise ValueError("Store belongs to a different session")
        return ReplSession(
            self._manager,
            self,
            store.store_handle,
            session_id=self._session_id,
            rpc_timeout=self._manager.rpc_timeout,
            line_editors=self.runtime_settings.line_editors,
            store=store,
        )

    def claim_eval(self, eval_session: EvalSession) -> None:
        active_eval = self._active_eval
        if active_eval is not None and active_eval is not eval_session:
            raise EvalStateBusyError("Session already has a live EvalState")
        self._active_eval = eval_session

    def release_eval(self, eval_session: EvalSession) -> None:
        if self._active_eval is eval_session:
            self._active_eval = None


# Backward-compatible alias
Nix = Session
