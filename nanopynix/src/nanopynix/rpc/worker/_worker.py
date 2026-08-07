"""Subprocess worker — Nix execution over gRPC (multiprocessing pipe or stdio).

Threading model
  Two threads with a clear boundary:

  * **Event loop thread** — gRPC handlers, H2 transport, WorkerBackchannel,
    log-relay task, primop-dispatcher task.
  * **Evaluator thread** — dedicated ``ThreadPoolExecutor(max_workers=1)``
    for the thread-confined EvalState and its Values.
  * **Store threads** — a bounded executor for concurrent Store operations.

  The event loop never blocks on Nix — handlers dispatch via
  ``executor.run()`` and the loop stays free for log relaying, primop RPC
  dispatch, and other gRPC calls.  ``HandleRegistry`` is locked for
  cross-thread access.  ``LogCollector`` is inherently thread-safe
  (``janus.Queue``).

Spawned by ``Session``'s ``rpc.client._pool.WorkerClient``.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import json
import os
import signal
import sys
import time
import traceback
from typing import TYPE_CHECKING, Any, cast

import anyio
import anyio.to_thread
from grpclib_transports.stdio import serve_stdio
from nanopynix_bindings import expr as nanopynix_expr, util as nanopynix_util
from nanopynix_proto.nix.common import LogEvent, LogLevel, NixLogEvent, RequestFinalized
from nanopynix_proto.nix.worker import (
    CloseStoreRequest,
    CloseStoreResponse,
    GetSettingsRequest,
    GetSettingsResponse,
    GetVerbosityRequest,
    GetVerbosityResponse,
    InitRequest,
    InitResponse,
    OpenStoreRequest,
    OpenStoreResponse,
    SetSettingsRequest,
    SetSettingsResponse,
    SetVerbosityRequest,
    SetVerbosityResponse,
    ShutdownRequest,
    ShutdownResponse,
    SubscribeLogsRequest,
    WorkerServiceBase,
)

from nanopynix._core._primops import import_primop_callable as _import_callable
from nanopynix._process_title import set_process_title, set_worker_title
from nanopynix._typechecking import BEARTYPING
from nanopynix._wire import (
    NIX_USER_CONF_FILES_ENV,
    WORKER_INIT_STATUS_OK,
    HandleKind,
)
from nanopynix.logging import LogCollector, LogOutbox, LogStreamEventKind, events_dropped_event
from nanopynix.models import PrimOpSpec
from nanopynix.namespace import enter_overlay_namespace
from nanopynix.rpc._status_details import NIX_STATUS_DETAILS_CODEC
from nanopynix.rpc.worker._grpc_util import wrap_service_handlers
from nanopynix.rpc.worker._state import WorkerState as WorkerState
from nanopynix.rpc.worker._worker_eval import EvalServiceHandler, close_eval_state, find_evals_by_store
from nanopynix.rpc.worker._worker_nix import NixThreadExecutor, abandoned_work_is_running
from nanopynix.rpc.worker._worker_primop import (
    ThreadedRpcPrimopBridge,
    rpc_primop_callback_factory as rpc_primop_callback_factory,  # type: ignore[reportPrivateUsage] -- internal module, required for primop callback factory
)
from nanopynix.rpc.worker._worker_store import StoreServiceHandler
from nanopynix.settings import DEFAULT_WORKER_MAX_CONCURRENCY

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncIterator, Collection

    from grpclib._typing import (
        IServable,  # type: ignore[reportPrivateUsage] -- named only in the cast in _stdio_main; see the comment there
    )
    from grpclib_transports import WorkerBackchannel

    from nanopynix.namespace import OverlayNamespace

# Re-export for the multiprocessing runner in _pool.py
__all__ = ["main", "worker_child_teardown", "worker_service_factory"]

_STORE_WORKERS = 4

# ── Primop registration ──────────────────────────────────────────────


def _register_primops(
    raw_specs: list[dict[str, Any]],
    rpc_bridge: Any = None,
) -> None:
    for raw in raw_specs:
        spec = PrimOpSpec.from_dict(raw)
        if spec.rpc:
            if rpc_bridge is None:
                raise RuntimeError(f"RPC primop {spec.name!r} registered without backchannel")
            callback = rpc_primop_callback_factory(rpc_bridge, spec.name, spec.arity)
        else:
            callback = _import_callable(spec.import_path)
        nanopynix_expr.register_primop(
            spec.name,
            spec.arity,
            spec.args,
            spec.doc,
            callback,
        )


def _install_worker_diagnostics(collector: LogCollector, outbox: LogOutbox) -> None:
    def _dump_worker_diagnostics(signum: int, _frame: Any) -> None:
        timestamp = time.monotonic()
        print(  # noqa: T201 -- SIGUSR1 signal handler; logging framework may be unsafe to call from signal context
            f"\n=== nanopynix worker diagnostics signal={signum} monotonic={timestamp:.6f} ===",
            file=sys.stderr,
            flush=True,
        )
        print(f"log_collector={collector.stats()}", file=sys.stderr, flush=True)  # noqa: T201 -- same signal-handler reason
        # Both halves of the log path, because a stall in either is invisible
        # from the other: a deep collector means the relay task is not running,
        # a deep outbox means the client is not reading.
        print(f"log_outbox={outbox.stats()}", file=sys.stderr, flush=True)  # noqa: T201 -- same signal-handler reason
        print("python_threads:", file=sys.stderr, flush=True)  # noqa: T201 -- same signal-handler reason
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        print("=== end nanopynix worker diagnostics ===", file=sys.stderr, flush=True)  # noqa: T201 -- same signal-handler reason

    signal.signal(signal.SIGUSR1, _dump_worker_diagnostics)


# ── WorkerService handler ────────────────────────────────────────────


@wrap_service_handlers
class WorkerServiceHandler(WorkerServiceBase):
    """Lifecycle handler: init, subscribe-logs, shutdown."""

    def __init__(self, state: WorkerState) -> None:
        self._state: WorkerState = state

    async def init(self, message: InitRequest) -> InitResponse:
        """Bootstrap Nix without splitting its global state across threads."""
        try:
            # **First, and before every other line of this handler.** Nix reads
            # `NIX_SSHOPTS` when a store opens its SSH connection, the proxy
            # variables when it fetches, and `TMPDIR` when it builds, so all of
            # those only need to precede the operation that reads them. The
            # binding that matters is `loadConfFile`, which runs inside
            # `initialize` below.
            #
            # A name Nix reads while `libnixstore` loads never arrives here at
            # all: the forkserver preloads this module, so the library is
            # loaded before a worker exists. The client refuses those names
            # rather than let them do nothing -- see nanopynix._env.
            #
            # Nothing puts this back. The worker process is disposable, which
            # is the same reason the `nix_conf` line below leaves its
            # assignment in place. `inproc.Session._restore_environment` states
            # the other half of the rule, for a process that is not.
            os.environ.update(message.env)
            settings = dict(message.settings)
            # NIX_USER_CONF_FILES must be in place before libstore reads
            # configuration, because `loadConfFile` is what reads it. It is an
            # input to Nix, not a side channel.
            #
            # The settings themselves do not travel through NIX_CONFIG any
            # more. They used to, because they were applied before
            # `initLibStore` and `loadConfFile` then overwrote them; the
            # environment variable existed only to win that race. They are now
            # applied after init, straight into globalConfig -- see
            # NixCore.initialize.
            if message.nix_conf is not None:
                os.environ[NIX_USER_CONF_FILES_ENV] = message.nix_conf

            if self._state.executor is None:
                raise RuntimeError("worker executor is unavailable")  # noqa: TRY301 -- guard clause intentionally caught by except block which prints traceback and re-raises
            if self._state.rpc_bridge is not None:
                # worker_service_factory() must stay synchronous (it is
                # handed to grpclib_transports as a BackchannelServiceFactory
                # callback invoked without await), so the bridge's
                # BlockingPortal -- whose __aenter__ needs a running event
                # loop and an await -- starts here instead, on the first
                # async operation the worker performs. Init always precedes
                # OpenEval/primop registration, so this is ready before any
                # primop could actually fire.
                await self._state.rpc_bridge.start()
            await self._state.run_request(
                request_id=message.request_id,
                executor=self._state.executor,
                operation=self._init_nix,
                args=(message, settings),
            )

            return InitResponse(status=WORKER_INIT_STATUS_OK)

        except Exception:
            traceback.print_exc(file=sys.stderr)
            # Re-raise so the gRPC framework propagates the error.
            raise

    def _init_nix(self, message: InitRequest, settings: dict[str, str]) -> None:
        """Run every Nix C++ initialization operation on the Nix thread."""
        # The features go to `initialize`, which enables them after
        # `init_libstore`. Enabling them here was a no-op on a host whose
        # `nix.conf` names `experimental-features`: `loadConfFile` writes the
        # whole set, so it discarded whatever this loop had inserted, and only
        # the `experimental-features` setting applied afterwards put them back.
        self._state.provenance = self._state.runtime.initialize(
            settings=settings,
            experimental_features=message.experimental_features,
            load_config=message.load_config,
            # None means "leave Nix's own default alone", which is what inproc
            # has always done. This used to force NOTICE, so the two engines
            # answered 2 and 3 to the same get_verbosity() call on an
            # otherwise identical session -- a difference in log volume that
            # nothing about running in a subprocess required.
            verbosity=None if message.verbosity is None else int(message.verbosity),
        )
        # Take the level now that `initialize` has published it. A worker that
        # was given no verbosity gets Nix's own compiled-in level, which is
        # what `get_default_verbosity` reports before anything sets it.
        self._state.verbosity = (
            int(message.verbosity) if message.verbosity is not None else self._state.runtime.get_default_verbosity()
        )
        self._state.nix_path = list(message.nix_path)

        primops_raw = [
            {
                "name": p.name,
                "arity": p.arity,
                "args": list(p.args),
                "doc": p.doc,
                "import_path": p.import_path,
                "rpc": p.rpc,
            }
            for p in message.primops
        ]
        if self._state.rpc_bridge is None:
            raise RuntimeError("worker rpc_bridge is unavailable")  # set by worker_service_factory before init
        _register_primops(primops_raw, rpc_bridge=self._state.rpc_bridge)

    async def open_store(self, message: OpenStoreRequest) -> OpenStoreResponse:
        if self._state.store_limiter is None:
            raise RuntimeError("worker store limiter is unavailable")
        store_handle, uri, store_dir = await self._state.run_request(
            request_id=message.request_id,
            limiter=self._state.store_limiter,
            operation=self._open_store,
            args=(message.uri,),
        )
        return OpenStoreResponse(
            store_handle=store_handle,
            uri=uri,
            store_dir=store_dir,
        )

    def _open_store(self, uri: str) -> tuple[int, str, str]:
        store = self._state.runtime.open_store(uri)
        handle = self._state.handles.allocate(store, HandleKind.STORE)
        store_uri = store.get_uri()
        self._state.named_store_uris[handle] = store_uri
        self._update_store_title()
        return handle, store_uri, store.get_store_dir()

    async def close_store(self, message: CloseStoreRequest) -> CloseStoreResponse:
        if self._state.executor is None:
            raise RuntimeError("worker executor is unavailable")
        # A forced close may have to destroy the thread-confined EvalState, so
        # retain the evaluator lane for this lifecycle operation.
        await self._state.run_request(
            request_id=message.request_id,
            executor=self._state.executor,
            operation=self._close_store,
            args=(message.store_handle, message.force),
        )
        return CloseStoreResponse()

    def _close_store(self, store_handle: int, force: bool = False) -> None:
        try:
            store = self._state.handles.get_store(store_handle)
        except KeyError:
            # Store close is idempotent: a client or session teardown may
            # repeat a successful forced close.
            return
        eval_handles = find_evals_by_store(self._state, store_handle)
        if eval_handles:
            if not force:
                raise RuntimeError("cannot close a store while its EvalState is open; call CloseEval first")
            for eval_handle in eval_handles:
                close_eval_state(self._state, eval_handle)
        store.close()
        self._state.handles.release(store_handle)
        if self._state.named_store_uris.pop(store_handle, None) is not None:
            self._update_store_title()

    async def get_verbosity(self, message: GetVerbosityRequest) -> GetVerbosityResponse:
        """Return the worker's current Nix logger verbosity.

        Read from a Nix thread, and not from the worker's own record of the
        level. The bindings hold the verbosity per thread, so what a Nix
        thread reports is what Nix would actually filter this worker's
        messages at. The two agree while ``run_request`` carries the level,
        and this door is where a caller would find out that they do not.
        """
        if self._state.executor is None:
            raise RuntimeError("worker executor is unavailable")
        verbosity = await self._state.run_request(
            request_id=message.request_id, executor=self._state.executor, operation=self._state.runtime.get_verbosity
        )
        return GetVerbosityResponse(verbosity=LogLevel(verbosity))

    async def set_verbosity(self, message: SetVerbosityRequest) -> SetVerbosityResponse:
        """Update this worker's Nix logger verbosity.

        The worker takes the level first, so the next request carries it. The
        dispatched call then publishes the same level to the threads Nix
        starts for itself, which no request ever reaches.
        """
        if self._state.executor is None:
            raise RuntimeError("worker executor is unavailable")
        level = int(message.verbosity)
        self._state.verbosity = level
        await self._state.run_request(
            request_id=message.request_id,
            executor=self._state.executor,
            operation=self._state.runtime.set_verbosity,
            args=(level,),
        )
        return SetVerbosityResponse(verbosity=LogLevel(level))

    async def get_settings(self, message: GetSettingsRequest) -> GetSettingsResponse:
        """Return the worker's effective Nix configuration, and where it came from.

        Unanswerable before this existed: the worker holds its own globals, in
        its own process, and nothing reported them back.
        """
        if self._state.executor is None:
            raise RuntimeError("worker executor is unavailable")
        settings = await self._state.run_request(
            request_id=message.request_id,
            executor=self._state.executor,
            operation=self._state.runtime.list_settings,
            args=(message.overridden_only,),
        )
        provenance = self._state.provenance
        return GetSettingsResponse(
            settings=dict(settings),
            from_config=dict(provenance.from_config),
            applied=dict(provenance.applied),
        )

    async def set_settings(self, message: SetSettingsRequest) -> SetSettingsResponse:
        """Apply global settings to this worker, and read each one back.

        The client refuses to send this while a store or an evaluator is open,
        because both read their settings at construction. That guard is on the
        client because only the client knows which objects the caller still
        holds; the worker applies what it is given.
        """
        if self._state.executor is None:
            raise RuntimeError("worker executor is unavailable")
        applied = await self._state.run_request(
            request_id=message.request_id,
            executor=self._state.executor,
            operation=self._state.runtime.apply_settings,
            args=(dict(message.settings),),
        )
        self._state.provenance = self._state.provenance.model_copy(
            update={"applied": {**self._state.provenance.applied, **dict(applied)}},
        )
        return SetSettingsResponse(applied=dict(applied))

    def _update_store_title(self) -> None:
        subname = " ".join(self._state.named_store_uris.values()) or self._state.worker_subname
        set_process_title(subname, project_name="nanopynix")

    async def subscribe_logs(self, message: SubscribeLogsRequest) -> AsyncIterator[LogEvent]:
        """Server-streaming RPC — send the log events the relay has buffered.

        This is the wire hop, and it is the one place in the worker that may
        wait: each yield becomes an HTTP/2 send, which parks when the client
        stops reading. It parks over :class:`~nanopynix.logging.LogOutbox`,
        which discards, so the wait never reaches the collector and never
        reaches Nix.

        The encoding happens in the relay rather than here, so that a parked
        send does not also stop the collector from draining.

        Deliberately not built on :class:`nanopynix.logging.CallbackBus` (the
        in-process pub-sub shared by ``inproc.Session`` and the client's
        ``WorkerClient``) — there is nothing to subscribe/unsubscribe here,
        only a single buffer serialized onto a gRPC channel. One subscriber
        only, for the same reason.
        """
        outbox = self._state.outbox
        if outbox is None:
            return

        try:
            while True:
                event = await outbox.get()
                if event is None:
                    break
                dropped = outbox.take_dropped()
                if dropped:
                    yield events_dropped_event(dropped)
                yield event
        except anyio.get_cancelled_exc_class():
            pass

    async def shutdown(self, message: ShutdownRequest) -> ShutdownResponse:
        """Acknowledge shutdown request.

        The actual process exit happens when the transport / pipe closes.
        """
        if self._state.executor is None:
            raise RuntimeError("worker executor is unavailable")
        await self._state.run_request(
            request_id=message.request_id, executor=self._state.executor, operation=lambda: None
        )
        # End the SubscribeLogs stream from this side, before the client stops
        # waiting on it. Without this the client's only way out is to cancel,
        # which resets the stream under a server handler that is still inside
        # its `async for` -- and grpclib logs that as "Failed to handle
        # cancellation" on the worker's stderr, which the parent inherits. The
        # events this drops were already being dropped by that cancellation.
        #
        # Into the collector, not the outbox: the sentinel must arrive behind
        # the events already queued there, and the relay is what moves those.
        if self._state.collector is not None:
            self._state.collector.send_sentinel()
        if self._state.rpc_bridge is not None:
            await self._state.rpc_bridge.stop()
        if self._state.owns_executor:
            self._state.executor.shutdown(wait=False)
        return ShutdownResponse()


# ── Factory ──────────────────────────────────────────────────────────

# The three handlers this module serves, named as themselves rather than as
# grpclib's `IServable`.
#
# `IServable` is the protocol grpclib's `Server` accepts, and it looks like
# the right annotation. It is not one that can be checked: it is a `Protocol`
# without `@runtime_checkable`, so beartype cannot build an `isinstance` test
# for it, warns, and leaves the function undecorated. Both functions annotated
# with it were therefore the only ones in `nanopynix` that no test checked at
# runtime, and each worker subprocess printed the warning into the captured
# output of whichever test started it.
#
# A plain assignment, not a PEP 695 `type` statement: the alias must be a real
# union object at import time for beartype to compile a check from it.
#
# The handlers satisfy `IServable` structurally, so `Server` still accepts
# them and the `cast` that used to launder the return type is gone.
WorkerServices = WorkerServiceHandler | StoreServiceHandler | EvalServiceHandler


def _ignore_terminal_interrupts() -> None:
    """Take this worker out of the terminal's Ctrl-C.

    A worker is a child of the client, so it shares the client's foreground
    process group. The terminal driver sends SIGINT to that whole group, and
    the worker used to die of it: ``asyncio.Runner`` installs a SIGINT handler
    that cancels the main task and re-raises ``KeyboardInterrupt``, which ends
    the process.

    Measured in the pynix REPL. Ctrl-C during an evaluation reached both
    processes. The REPL cancelled the evaluation and returned to its prompt,
    exactly as intended, and its worker died at the same moment, which left
    the session unusable and printed a grpclib traceback over the prompt.

    Ignoring the signal is what the ownership rule already says: one Session
    owns one worker, and ``WorkerClient`` is the only thing that may stop it.
    A terminal is not ``WorkerClient``. Cancellation still reaches the worker,
    over the wire, as gRPC handler cancellation -- which is the path that
    turns into Nix's interrupt hook, and the whole point of #37.

    Installed before ``asyncio.Runner`` can look at the handler, so the runner
    leaves SIGINT alone. SIGTERM and SIGKILL are untouched: those are how
    ``_stop_worker_process`` really does stop a worker.
    """
    with contextlib.suppress(ValueError, OSError):
        # ValueError: not the main thread of this process, which only happens
        # in an embedded test harness. Nothing to protect there.
        signal.signal(signal.SIGINT, signal.SIG_IGN)


def worker_service_factory(
    backchannel: WorkerBackchannel | None = None,
    *,
    executor: NixThreadExecutor | None = None,
    store_limiter: anyio.CapacityLimiter | None = None,
    namespace: OverlayNamespace | None = None,
) -> list[WorkerServices]:
    """Create service handlers with a shared WorkerState.

    Must be called *inside* the worker process (before Nix init so that
    the logger is installed early).

    When *namespace* is given, this is also the point where the worker enters
    its own user namespace and mounts the overlay store. It happens here
    because this is the last moment the process has one thread, which
    ``unshare(CLONE_NEWUSER)`` requires -- see :mod:`nanopynix.namespace`. The
    executor below starts the second thread.

    Sets up:

    * ``LogCollector`` + log-relay task (event loop thread)
    * evaluator ``NixThreadExecutor`` (dedicated single-worker thread)
    * Store ``anyio.CapacityLimiter`` (bounds concurrent Store work on
      anyio's shared thread pool)
    * ``ThreadedRpcPrimopBridge`` (Nix→event-loop primop dispatch; its
      ``BlockingPortal`` is started later, from ``WorkerServiceHandler.init``
      -- this factory must stay synchronous to satisfy grpclib_transports'
      ``BackchannelServiceFactory`` contract)
    """
    _ignore_terminal_interrupts()

    # First, before the logger and before the executor thread: the namespace
    # call needs this process to still be single-threaded.
    if namespace is not None:
        enter_overlay_namespace(namespace)

    collector = LogCollector()
    outbox = LogOutbox()
    nanopynix_util.install_logger(collector.callback)
    with contextlib.suppress(RuntimeError, ValueError):
        _install_worker_diagnostics(collector, outbox)

    state = WorkerState()
    state.worker_subname = set_worker_title()
    state.collector = collector
    state.outbox = outbox

    state.executor = NixThreadExecutor() if executor is None else executor
    state.owns_executor = executor is None
    state.store_limiter = anyio.CapacityLimiter(_STORE_WORKERS) if store_limiter is None else store_limiter

    if backchannel is not None:
        state.rpc_bridge = ThreadedRpcPrimopBridge(backchannel)

    # Before the handlers exist, so the relay is draining the collector before
    # anything can serve a request into it.
    _start_log_relay(state)

    return [
        WorkerServiceHandler(state),
        StoreServiceHandler(state),
        EvalServiceHandler(state),
    ]


# ── Async runner (for grpclib-transports multiprocessing mode) ───────


def _worker_state_of(handlers: list[WorkerServices]) -> WorkerState:
    """Return the state the three handlers share.

    All three hold the same object, so the first one will do. The narrowing is
    a real check now that the list is typed as the concrete handlers rather
    than as grpclib's protocol.
    """
    first = handlers[0]
    if not isinstance(first, WorkerServiceHandler):
        raise TypeError(f"worker_service_factory must build the worker handler first, got {type(first).__name__}")
    return first._state  # type: ignore[reportPrivateUsage] -- one worker module, three handler classes  # noqa: SLF001


async def _relay_logs(collector: LogCollector, outbox: LogOutbox) -> None:
    """Move every collector event into the outbox, and never wait for the wire.

    The task that makes the rule at the top of ``nanopynix.logging`` true. It
    encodes each event and hands it on, so the collector stays near empty
    whatever the client is doing, and the Nix thread never finds a full queue.

    ``SubscribeLogs`` used to consume the collector itself. That put an HTTP/2
    send between the collector and its drain, so a client that stopped reading
    stopped Nix. See issue #13.
    """

    def _announce_drops() -> None:
        dropped = collector.take_dropped()
        if dropped:
            outbox.put(events_dropped_event(dropped))

    try:
        async for event in collector.stream():
            if event is None:
                # `LogCollector.stream` breaks on the sentinel itself and never
                # yields it. The guard stays because the unpack below would
                # raise on a collector that behaves differently.
                break
            # Before the event, so the notice sits where the loss happened and
            # the reader can tell which part of the log is incomplete.
            _announce_drops()
            kind, request_id, *payload = event
            if kind == LogStreamEventKind.NIX:
                action, *args = payload
                outbox.put(
                    LogEvent(
                        request_id=request_id,
                        nix_log=NixLogEvent(action=action, args_json=json.dumps(args, default=str)),
                    )
                )
            elif kind == LogStreamEventKind.FINALIZED:
                outbox.put(LogEvent(request_id=request_id, request_finalized=RequestFinalized()))
    except anyio.get_cancelled_exc_class():
        raise
    except Exception:
        # Nothing awaits this task, so an exception here would otherwise stop
        # every log event with no other symptom at all. Reported the way the
        # rest of this module reports: to the stderr the parent inherits.
        print("nanopynix worker: the log relay failed", file=sys.stderr, flush=True)  # noqa: T201 -- see above
        traceback.print_exc(file=sys.stderr)
    finally:
        # Nothing here awaits, so it also runs under the cancellation that
        # `_shutdown_worker` delivers. A trailing burst of drops is announced
        # before the sentinel ends the stream.
        _announce_drops()
        outbox.put(None)


def _start_log_relay(state: WorkerState) -> None:
    """Start the relay task and record it on the shared state.

    ``worker_service_factory`` calls this, and that is the only correct place:
    the factory is the one point every serving path goes through.
    ``run_worker`` and ``_stdio_main`` are not -- the multiprocessing child
    runs ``_run_multiprocessing_worker_with_backchannel``, which calls the
    factory itself, and the in-process L3 transport does the same. Starting
    the relay in the runners left the real worker with an outbox that nothing
    ever filled, and ``SubscribeLogs`` delivered nothing at all.

    The factory is synchronous, which the ``BackchannelServiceFactory``
    contract requires, but every caller invokes it from inside
    ``asyncio.run``. So a running loop is a precondition, not an assumption to
    work around: a silent skip is exactly the failure above.
    """
    collector, outbox = state.collector, state.outbox
    if collector is None or outbox is None:
        raise RuntimeError("worker_service_factory did not build the log collector and outbox")
    state.log_task = asyncio.create_task(_relay_logs(collector, outbox), name="nanopynix-worker-log-relay")


async def _shutdown_worker(handlers: list[WorkerServices]) -> None:
    """Tear down the shared `WorkerState` after a transport closes -- shared by
    `worker_child_teardown` (multiprocessing) and `_stdio_main` (stdio), which
    otherwise hand-repeated this identically.

    Every step is idempotent, because the polite path already ran most of them:
    a client that closes cleanly sends `CloseEval` for each evaluator and then
    `Shutdown`, and those handlers do this work themselves. This function is
    what runs when the client asked for none of it -- a cancelled close, a
    `Shutdown` that timed out, a parent that died.
    """
    worker_state: WorkerState = _worker_state_of(handlers)

    # First, and the reason this function has to run at all. Each evaluator
    # owns a dedicated thread that `_enter_evaluator_thread` registered with
    # the Boehm collector, and `close_eval_state` is the only thing that runs
    # the matching `_exit_evaluator_thread` on it. Without this, the thread is
    # joined by `concurrent.futures`'s own atexit hook and exits still
    # registered, and a later collection then signals a dead tid. See the
    # account in `_core/_nix_executor.py`.
    #
    # `anyio.to_thread`, and not the shared executor: `close_eval_state`
    # blocks on the evaluator's thread, so it can run on neither the event
    # loop nor that thread. The shared executor would serve, but `Shutdown`
    # may already have closed it, and then this teardown would work only for
    # the client that did not need it.
    for eval_handle, _entry in worker_state.handles.iter_evals():
        await anyio.to_thread.run_sync(close_eval_state, worker_state, eval_handle)

    collector = worker_state.collector
    if collector is not None:
        collector.close()
    log_task = worker_state.log_task
    if log_task is not None:
        log_task.cancel()
        with contextlib.suppress(anyio.get_cancelled_exc_class()):
            await log_task
    if worker_state.rpc_bridge is not None:
        await worker_state.rpc_bridge.stop()
    if worker_state.executor is not None:
        worker_state.executor.shutdown(wait=True)


async def worker_child_teardown(handlers: Collection[WorkerServices]) -> None:
    """End a multiprocessing worker child, after ``serve_h2`` returns.

    ``_pool.py`` hands this to ``multiprocessing_worker_with_backchannel`` as
    its ``child_teardown``, which is the only hook the child has after the
    transport closes. The forkserver pickles it, so it is module-level.

    This replaces ``run_worker``, which said the same thing and had no caller:
    it wrapped the serve as well, but the transport owns that.

    ``Collection[WorkerServices]`` and not the transport's
    ``Collection[IServable]``: that protocol is not ``@runtime_checkable``, so
    naming it would leave this whole function silently unchecked by beartype.
    The ledger in ``tests/nanopynix/test_beartype_instrumentation.py`` records
    the same mistake, and the same correction. The call site carries the one
    cast this costs, because a parameter is contravariant.

    ``Collection``, because the transport passes a tuple and ``_stdio_main``
    passes a list.
    """
    await _shutdown_worker(list(handlers))
    # After the teardown, not before: an evaluator that answers its interrupt
    # is closed properly above, and only a thread that never stopped reaches
    # this. See the docstring for why a worker may end itself this way.
    _exit_now_if_nix_will_not_stop()


# ── Stdio entry point (console_script / ``python -m nanopynix._worker``) ──


def main() -> None:
    """Stdio entry point — serves gRPC over stdin/stdout.

    This is the ``nanopynix-worker`` console_script target and also the
    fallback for ``sys.executable -m nanopynix._worker``.
    """
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_stdio_main())
    _exit_now_if_nix_will_not_stop()


def _exit_now_if_nix_will_not_stop() -> None:
    """End this process at once when an abandoned Nix thread is still running.

    A cancelled operation that Nix never answered leaves its thread inside
    libexpr, allocating from the Boehm collector. A collection that races
    Python's finalization aborts with "Signals delivery fails constantly", and
    a normal exit would instead block for ever joining that thread.

    A worker may do this because a worker is disposable: the client already
    treats a dead worker as ``WorkerDiedError`` and replaces it. ``nanopynix``
    itself must never do this, because there the process belongs to the caller.
    That difference is forced by process isolation, and it is the one place
    where the RPC engine really can do something the in-process engine cannot.
    """
    if not abandoned_work_is_running():
        return
    print(  # noqa: T201 -- stderr is this worker's log channel; the logging framework may already be torn down
        "nanopynix worker: exiting immediately, a cancelled Nix operation never "
        "stopped. The client will see the worker die and replace it.",
        file=sys.stderr,
    )
    sys.stdout.flush()
    sys.stderr.flush()
    # os._exit and not sys.exit: sys.exit unwinds into the teardown that
    # joins the stuck thread, which is the hang being avoided.
    os._exit(0)


async def _stdio_main() -> None:
    handlers = worker_service_factory()
    await serve_stdio(
        # The one place the protocol has to be named, because `serve_stdio`
        # declares `list[IServable]` and `list` is invariant. Its sibling
        # `serve_multiprocessing_endpoint` declares `Collection[IServable]`,
        # which is covariant and needs no cast -- so this is an asymmetry in
        # grpclib-transports rather than anything the handlers lack. A cast
        # here, and not an annotation, keeps beartype able to check both
        # functions above; see UNDECORATABLE in
        # tests/nanopynix/test_beartype_instrumentation.py.
        cast("list[IServable]", handlers),
        max_concurrency=DEFAULT_WORKER_MAX_CONCURRENCY,
        status_details_codec=NIX_STATUS_DETAILS_CODEC,
    )
    await _shutdown_worker(handlers)


if __name__ == "__main__":
    main()
