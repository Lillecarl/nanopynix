"""Session manager — lifecycle, configuration, and entry point for all facades.

Manages a single subprocess worker via ``WorkerClient``.  The worker is an
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

import logging
import uuid
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from nanopynix_proto.nix.common import LogEvent as LogEventProto
from nanopynix_proto.nix.common import LogLevel

from nanopynix._core._primops import to_primop_specs
from nanopynix._process_title import set_manager_title
from nanopynix._wire import DEFAULT_STORE_URI
from nanopynix.logging import LogCapture
from nanopynix.models import LogEvent
from nanopynix.rpc.client._pool import WorkerClient
from nanopynix.rpc.client._session import EvalSession, ReplSession
from nanopynix.rpc.client.store import Store, StoreHandle
from nanopynix.settings import (
    NanopynixSettings,
    NixEvalSettings,
    NixFetchSettings,
    NixSettings,
    normalize_nix_path,
    normalize_nix_settings,
)
from nanopynix.verbosity import LogLevelInput, normalize_log_level

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from os import PathLike

    from nanopynix.models import PrimOpSpec

logger = logging.getLogger(__name__)

_GRACEFUL_CLOSE_TIMEOUT_SECONDS = 60.0
"""How long close() spends handing worker-side resources back before giving up.

A backstop, and deliberately shorter than what it bounds: each handle-drop RPC
inside it goes out at ``rpc_timeout``, 300 seconds by default, so a single
wedged evaluator or store can blow this budget on its own. That is the intent
-- dropping a handle is a bookkeeping call that takes milliseconds when the
worker is answering at all, so a minute of it means the worker is not coming
back and close() should stop asking. The worker still dies either way: it is
stopped outside this deadline, in a ``finally``, by a ``WorkerClient.close``
that shields its own teardown.

Not a caller-facing knob -- see the ``Session.close:params`` entry in
tests/nanopynix/test_engine_parity.py for why rpc's close takes no parameters
where inproc's takes three."""


class Session:
    """Session runtime — manages a single subprocess worker.

    A Session owns one thread-confined EvalState at a time. Its Store facade is
    independent of that evaluator: concurrent Store calls are dispatched to a
    bounded worker-side Store pool and may overlap with evaluator work.

    Usage::

        async with Session(
            store_uri="daemon",
            settings=NixSettings(max_jobs=4),
        ) as session:
            async with session.store() as store:
                info = await store.query_path_info(str(sp))
    """

    def __init__(  # noqa: PLR0913 tracked complexity/arg-count debt, see TODO.md
        self,
        *,
        store_uri: str = DEFAULT_STORE_URI,
        nix_conf: Path | None = None,
        load_config: bool = True,
        settings: NixSettings | PathLike[str] | str | None = None,
        experimental_features: list[str] | None = None,
        verbosity: LogLevelInput | None = None,
        nix_path: str | Sequence[str] | None = None,
        primops: Sequence[PrimOpSpec | Mapping[str, Any]] | None = None,
        primop_callables: Mapping[str, Callable[..., Any]] | None = None,
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
        self._store_uri = store_uri
        worker_settings = nix_settings.to_worker_settings()
        self._manager = WorkerClient(
            store_uri=store_uri,
            nix_conf=nix_conf,
            load_config=load_config,
            settings=worker_settings,
            experimental_features=list(nix_settings.experimental_features or []),
            verbosity=normalize_log_level(verbosity) if verbosity is not None else None,
            nix_path=normalize_nix_path(nix_path),
            primops=to_primop_specs(primops),
            primop_callables=dict(primop_callables) if primop_callables is not None else None,
            worker_oom_score_adj=worker_oom_score_adj,
            rpc_timeout=nanopynix_settings.rpc_timeout,
            shutdown_timeout=nanopynix_settings.shutdown_timeout,
        )
        self._session_id = uuid.uuid4().hex
        self._evals: set[EvalSession] = set()
        # Weak, unlike _evals: an evaluator leases a worker-side slot the
        # session must give back, so it is owned; a store is just a facade the
        # caller may drop without closing, and holding it alive to close it
        # later would be the leak rather than the fix. inproc keeps a strong set
        # and discards on Store.close, which needs the store to be closed to
        # stop accumulating; this does not.
        self._stores: weakref.WeakSet[Store] = weakref.WeakSet()

    def store(self, uri: str | None = None) -> Store:
        """Create an ergonomic Store facade for store operations.

        Usage::

            async with session.store() as store:
                info = await store.query_path_info(str(sp))
                results = await store.build_paths_with_results([str(sp)])

        :attr:`Store.rpc` reaches the generated request/response API directly,
        and is deliberately not demonstrated here: the facade now wraps every
        StoreService RPC, so there is nothing it can do that a method above
        cannot. It stays as the escape hatch for a future RPC that has not
        grown a method yet.

        The store carries this session's ID — passing it to
        ``Eval`` from a different session raises ``ValueError``. Opening the
        handle adds its URI to the worker's process title.
        """
        selected_uri = self._store_uri if uri is None else uri
        store = Store(
            StoreHandle(
                self._manager,
                selected_uri,
                self._session_id,
                self._manager.rpc_timeout,
            ),
        )
        self._stores.add(store)
        return store

    async def open(self) -> None:
        """Spawn the worker subprocess."""
        await self._manager.open()

    async def close(self) -> None:
        """Shut down the worker.

        Every resource this session handed out gets a chance to close, whatever
        the ones before it did. Failures are collected and raised together --
        one on its own, more as a :exc:`BaseExceptionGroup` -- exactly as
        ``inproc.Session.close`` reports them, and for the reason it does: the
        first evaluator to fail must not be the reason the rest, and the worker
        behind them, are left as they are.

        Raises :class:`TimeoutError` instead if handing those resources back
        runs past :data:`_GRACEFUL_CLOSE_TIMEOUT_SECONDS`.

        The worker process is gone in all three cases. It is stopped in a
        ``finally``, outside the deadline, by a ``WorkerClient.close`` that
        shields its own teardown -- so every failure here means "shutdown was
        not clean", never "shutdown did not happen".
        """
        errors: list[BaseException] = []

        async def close_resource(operation: Awaitable[None]) -> None:
            try:
                await operation
            except Exception as exc:
                errors.append(exc)

        try:
            # Evaluators first, then the stores they were bound to -- inproc's
            # order, for the same reason: a store cannot be closed while an
            # evaluator holds it. Closing the stores at all is new here. They
            # used to be left open, so a facade outliving its session reported
            # "session is not open" rather than the truth, which is that its
            # store is closed.
            with anyio.fail_after(_GRACEFUL_CLOSE_TIMEOUT_SECONDS):
                for eval_session in tuple(self._evals):
                    await close_resource(eval_session.close())
                for store in tuple(self._stores):
                    await close_resource(store.close())
        finally:
            # Unconditional, and outside the deadline. Both matter: a raising
            # evaluator used to abandon the close here, and the worker it left
            # running was a live Nix store connection nothing in this process
            # remembered. `_manager.close()` is bounded on its own -- the
            # Shutdown RPC at ``shutdown_timeout``, the teardown internally --
            # so it needs no deadline from here, and putting it under this one
            # would only mean an expired scope cancelling the polite half of a
            # shutdown that is about to happen the impolite way instead.
            await self._manager.close()

        # Cancellation is not collected, unlike inproc, which catches
        # BaseException here. Wrapping it would strip its meaning: the scope it
        # belongs to would never see it, so `fail_after` above could not turn
        # its own expiry into a TimeoutError -- the bug this file already had
        # once, in WorkerClient.close. Letting it through costs nothing, since
        # the worker is stopped in the finally either way.
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("errors closing rpc Session", errors)

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
            yield LogEvent.from_proto(raw)

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

    def eval(
        self,
        store: Store,
        *,
        build_store: Store | None = None,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
    ) -> EvalSession:
        """Create an eval-session context manager over this session's worker.

        Usage::

            async with session.store() as store:
                async with session.eval(store) as eval:
                    root = await eval.file("/path/to/flake.nix")

        Args:
            store: Open Store to use for this eval state. If it belongs to
                   a different session, raises ValueError.
            build_store: Optional second store to build into while evaluating
                   against ``store``. Same guard as ``store``.
            eval_settings: Construction-time evaluator settings for this
                   ``EvalSession`` alone (e.g. ``pure_eval``, ``nix_path``).
            fetch_settings: Construction-time fetcher settings for this
                   ``EvalSession`` alone.

        Returns an ``EvalSession`` context manager. All exported handles are
        released on exit. Any number of ``EvalSession``/``ReplSession``
        instances may be open concurrently — each gets its own dedicated Nix
        thread in the worker. Store operations remain available and may run
        concurrently alongside them.
        """
        self._require_own_stores(store, build_store)
        return EvalSession(
            self._manager,
            self,
            store.store_handle,
            session_id=self._session_id,
            rpc_timeout=self._manager.rpc_timeout,
            store=store,
            build_store=build_store,
            eval_settings=eval_settings,
            fetch_settings=fetch_settings,
        )

    def repl(
        self,
        store: Store,
        *,
        build_store: Store | None = None,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
        line_editors: Sequence[str] | None = None,
    ) -> ReplSession:
        """Create a persistent Nix REPL scope over ``store``.

        Bindings entered through :meth:`ReplSession.line` remain available
        until the returned context manager exits.

        Any number may be open at once, as with :meth:`eval` -- each owns its
        own ``EvalState`` in the worker, and Nix allows one REPL scope per
        ``EvalState``. (This previously claimed to be "this session's one
        permitted" REPL; nothing enforced that, and
        ``test_session_allows_concurrent_eval_states`` opens a second.)

        A ``ReplSession`` is an ``EvalSession``, so it takes the same
        construction-time settings :meth:`eval` does -- previously it took
        none, which left a REPL's evaluator the one evaluator on this engine
        that could not be configured.

        ``line_editors`` is per-call, as on ``inproc``, and falls back to this
        session's ``runtime_settings`` when omitted. It used to be readable
        only from ``runtime_settings``, so a caller wanting two REPLs with
        different editors had to open two workers.
        """
        self._require_own_stores(store, build_store)
        return ReplSession(
            self._manager,
            self,
            store.store_handle,
            session_id=self._session_id,
            rpc_timeout=self._manager.rpc_timeout,
            line_editors=self.runtime_settings.line_editors if line_editors is None else line_editors,
            store=store,
            build_store=build_store,
            eval_settings=eval_settings,
            fetch_settings=fetch_settings,
        )

    def _require_own_stores(self, store: Store, build_store: Store | None) -> None:
        """Reject stores opened by a different Session before an evaluator binds them.

        Mirrors ``inproc.Session._require_own_stores`` -- same check, same two
        messages, so a cross-session mix-up reads the same on either engine.
        """
        if store._session_id != self._session_id:  # type: ignore[reportPrivateUsage] -- cross-session guard on internal ID  # noqa: SLF001
            raise ValueError("Store belongs to a different session")
        if build_store is not None and build_store._session_id != self._session_id:  # type: ignore[reportPrivateUsage] -- cross-session guard on internal ID  # noqa: SLF001
            raise ValueError("build_store belongs to a different session")

    def claim_eval(self, eval_session: EvalSession) -> None:
        self._evals.add(eval_session)

    def release_eval(self, eval_session: EvalSession) -> None:
        self._evals.discard(eval_session)


# Backward-compatible alias
Nix = Session
