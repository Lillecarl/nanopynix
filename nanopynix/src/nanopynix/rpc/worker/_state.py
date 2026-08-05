"""The state that a worker's three service handlers share.

A neutral home, so that each handler can name the type it holds. ``_worker.py``
imports ``_worker_eval`` and ``_worker_store``, so a handler in either of those
modules cannot import ``WorkerState`` back from ``_worker.py``. Moving the
shared type to a module that neither imports is what ``AGENTS.md`` names as
the correction for such a cycle, and it is what replaces ``state: Any`` on the
handlers with a type the checker can read.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
from nanopynix_bindings import util as nanopynix_util
from nanopynix_proto.nix.common import LogLevel

from nanopynix._core._objects import CoreRuntime
from nanopynix._typechecking import BEARTYPING
from nanopynix.rpc.worker._handle_registry import HandleRegistry
from nanopynix.settings import SettingsProvenance

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable

    from nanopynix._core._nix_executor import NixThreadExecutor
    from nanopynix.logging import LogCollector, LogOutbox
    from nanopynix.rpc.worker._worker_primop import ThreadedRpcPrimopBridge


class WorkerState:
    """Shared mutable state held by all three service handlers.

    Thread confinement:
      * ``collector`` — both threads (already thread-safe via ``janus.Queue``)
      * ``outbox`` — event loop only (a plain deque, no locking)
      * ``log_task`` — event loop only; the relay that drains ``collector``
        into ``outbox``. See ``_start_log_relay``.
      * ``handles`` — both threads (locked ``HandleRegistry``); each open
        evaluator lives here as an ``EvalEntry`` (kind ``"eval"``), owning its
        own dedicated Nix thread — see ``_worker_eval.py``.
      * ``executor`` — event loop only. Process-global Nix operations only
        (``Init``, ``GetVerbosity``/``SetVerbosity``, the store-close
        bookkeeping in ``_close_store``) — evaluator work runs on each
        evaluator's own executor instead.
      * ``store_limiter`` — event loop only (bounds concurrent Store work
        dispatched via ``anyio.to_thread.run_sync``; unlike ``executor``, a
        ``CapacityLimiter`` has no thread-affinity requirement and no
        shutdown lifecycle of its own to own/release)
      * ``rpc_bridge`` — Nix thread reads, event loop writes (internal locking)
    """

    def __init__(self) -> None:
        self.collector: LogCollector | None = None
        self.outbox: LogOutbox | None = None
        self.log_task: asyncio.Task[None] | None = None
        self.handles: HandleRegistry = HandleRegistry()
        self.runtime = CoreRuntime()
        self.executor: NixThreadExecutor | None = None
        self.store_limiter: anyio.CapacityLimiter | None = None
        self.owns_executor = True
        self.rpc_bridge: ThreadedRpcPrimopBridge | None = None
        self.nix_path: list[str] = []
        self.worker_subname: str = "worker"
        self.named_store_uris: dict[int, str] = {}
        #: Where this worker's configuration came from, recorded by ``Init``.
        #: Kept so ``GetSettings`` can tell a host value from one this session
        #: applied, which a caller has no other way to learn across the wire.
        self.provenance = SettingsProvenance()
        #: The level a request logs at when it names no level of its own. The
        #: bindings hold the verbosity per thread, and Store work runs on
        #: anyio's shared thread pool, so a thread carries no level of its own
        #: that would survive to the next request. ``Init`` resolves this.
        #:
        #: An evaluator that called ``SetEvalVerbosity`` overrides it, per
        #: request — see ``EvalEntry.verbosity``. Store requests have no such
        #: override, because a store is session-scoped.
        self.verbosity: int = int(LogLevel.INFO)

    async def run_request(  # noqa: PLR0913 -- five request-local facts, each independent: what to run, what to run it with, which of the two dispatch mechanisms, and at which level. Grouping them would hide that executor and limiter are mutually exclusive, which the body enforces
        self,
        *,
        request_id: int,
        operation: Callable[..., Any],
        args: tuple[Any, ...] = (),
        executor: NixThreadExecutor | None = None,
        limiter: anyio.CapacityLimiter | None = None,
        verbosity: int | None = None,
    ) -> Any:
        """Run a unary operation with its request-local logger context installed.

        Exactly one of ``executor`` (a dedicated-thread ``NixThreadExecutor``,
        for evaluator-affine work) or ``limiter`` (an
        ``anyio.CapacityLimiter`` bounding ``anyio.to_thread.run_sync`` calls,
        for Store work) must be given -- these are two distinct dispatch
        mechanisms, not variants of one shared interface.

        ``verbosity`` is the level this one request logs at. An evaluator that
        holds a level of its own passes it; everything else leaves it out and
        gets ``self.verbosity``.
        """
        collector = self.collector
        if request_id <= 0:
            if collector is not None:
                collector.request_finalized(request_id)
            raise ValueError("request_id must be positive")
        if (executor is None) == (limiter is None):
            raise ValueError("run_request requires exactly one of executor or limiter")

        # Read before the closure runs, so the request logs at the level that
        # was in force when it was dispatched.
        level = self.verbosity if verbosity is None else verbosity

        def _run() -> Any:
            previous_id = nanopynix_util.get_logger_request_id()
            previous_verbosity = nanopynix_util.get_verbosity()
            nanopynix_util.set_logger_request_id(request_id)
            nanopynix_util.set_verbosity(level)
            try:
                return operation(*args)
            finally:
                if collector is not None:
                    collector.request_finalized(request_id)
                nanopynix_util.set_logger_request_id(previous_id)
                nanopynix_util.set_verbosity(previous_verbosity)

        if executor is not None:
            return await executor.run(_run)
        return await anyio.to_thread.run_sync(_run, limiter=limiter)

    def log(self, action: str, *args: object) -> None:
        """Emit a diagnostic in the current executor thread's request context."""
        if self.collector is not None:
            self.collector.callback(nanopynix_util.get_logger_request_id(), action, *args)
