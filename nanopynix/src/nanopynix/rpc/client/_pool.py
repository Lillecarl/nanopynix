"""Multiprocessing worker — Nix execution backend via gRPC over pipe transport.

A single forkserver subprocess runs an independent Nix process with its own
Store, logger, and globals.  Communication is gRPC over a multiprocessing pipe
pair via grpclib-transports.

Evaluator calls remain serial because an ``EvalState`` is thread-confined. Store
calls may be in flight concurrently: the worker dispatches them to its bounded
Store executor.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import anyio.to_thread
from grpclib.exceptions import GRPCError, StreamTerminatedError
from grpclib_transports.multiprocessing import multiprocessing_worker_with_backchannel
from nanopynix_proto.nix.common import PrimOpSpec as PrimOpSpecPB
from nanopynix_proto.nix.eval import EvalServiceStub
from nanopynix_proto.nix.store import StoreServiceStub
from nanopynix_proto.nix.worker import (
    GetSettingsRequest,
    GetSettingsResponse,
    GetVerbosityRequest,
    InitRequest,
    SetSettingsRequest,
    SetVerbosityRequest,
    ShutdownRequest,
    SubscribeLogsRequest,
    WorkerServiceStub,
)

from nanopynix._typechecking import BEARTYPING
from nanopynix._wire import DEFAULT_STORE_URI, WORKER_INIT_STATUS_OK
from nanopynix.exceptions import (
    SessionClosedError,
    WorkerDiedError,
    WorkerSignaledError,
    exception_from_wire,
    from_response,
    signal_name,
)
from nanopynix.logging import ACTIVE_LOG_CAPTURES, BusSubscription, CallbackBus, bus_log_stream
from nanopynix.namespace import probe_namespace_support
from nanopynix.rpc._status_details import NIX_STATUS_DETAILS_CODEC, unpack_error_details
from nanopynix.rpc.client._manager import ManagerPrimopServiceHandler
from nanopynix.rpc.worker._worker import worker_child_teardown, worker_service_factory
from nanopynix.settings import (
    DEFAULT_RPC_TIMEOUT_SECONDS,
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    DEFAULT_WORKER_MAX_CONCURRENCY,
    DEFAULT_WORKER_PRELOAD,
)

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from grpclib_transports.multiprocessing import ChildTeardown
    from nanopynix_proto.nix.common import LogLevel

    from nanopynix.logging import LogCallback
    from nanopynix.models import LogEvent, PrimOpSpec
    from nanopynix.namespace import OverlayNamespace

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
_OOM_SCORE_ADJ_MIN = -1000
_OOM_SCORE_ADJ_MAX = 1000

_LOG_DRAIN_TIMEOUT_SECONDS = 2.0
"""How long close() lets the log stream end by itself before cancelling it.

Module-private rather than a NixSettings field: this bounds a teardown race
against a worker that already acknowledged Shutdown, not anything a caller
would tune. Cancelling early is safe, just noisy -- see close()."""

_WORKER_TEARDOWN_TIMEOUT_SECONDS = 10.0
"""How long close() lets the exit stack tear the worker down before killing it.

grpclib_transports' teardown is ``await channel.aclose()`` then
``await _stop_process(proc)``, in that order, so a channel that never finishes
closing keeps the terminate/kill from ever running. This bounds the first half
so the second is always reachable; see close()'s teardown block. Module-private
for the same reason as _LOG_DRAIN_TIMEOUT_SECONDS -- it bounds a teardown
pathology, not anything a caller would tune."""

_WORKER_JOIN_TIMEOUT_SECONDS = 5.0
"""How long _stop_worker_process waits for a terminate before escalating to kill."""

_WORKER_REAP_TIMEOUT_SECONDS = 2.0
"""How long _reap_worker waits for the exit status of a worker that just died.

The pipe breaks when the child's file descriptors close, which is the instant
it dies. The exit status arrives later: the forkserver reaps the child, calls
``os.waitstatus_to_exitcode`` and writes the signed value to this process, and
``Process.exitcode`` returns ``None`` until then. So the two events are
genuinely ordered, and reading the status without waiting would usually get
nothing.

Two seconds because a worker death is not a hot path. It happens once per
session at most, and only when something already went wrong."""

# ════════════════════════════════════════════════════════════════════
# gRPC error helper
# ════════════════════════════════════════════════════════════════════


async def _grpc_call(coro: Any) -> Any:
    """Execute a gRPC stub call, converting GRPCError to the worker's exception.

    Two resolvers, best information first. ``exception_from_wire`` reads the
    ``ErrorIdentity`` the worker put in the status trailer, which names the
    class as a field. ``from_response`` reads the ``"TypeName: "`` prefix on
    the status message, which is all a peer without the codec can send, and
    which recovers a Nix C++ class and nothing else.

    Usage::

        resp = await _grpc_call(stub.method(request, timeout=...))
    """
    try:
        return await coro
    except GRPCError as exc:
        message = exc.message or str(exc)
        received = unpack_error_details(exc.details)
        resolved = exception_from_wire(
            nix_type=received.nix_type,
            class_name=received.class_name,
            msg=message,
            raw=received.raw,
            info=received.info,
        )
        if resolved is not None:
            raise resolved from exc
        raise from_response("Unknown", message, raw=received.raw, info=received.info) from exc
    except (StreamTerminatedError, ConnectionError) as exc:
        raise WorkerDiedError(str(exc)) from exc


def _clamp_oom_score_adj(value: int) -> int:
    return min(_OOM_SCORE_ADJ_MAX, max(_OOM_SCORE_ADJ_MIN, value))


def _write_oom_score_adj(pid: int, value: int, *, proc_root: Path = Path("/proc")) -> None:
    path = proc_root / str(pid) / "oom_score_adj"
    path.write_text(f"{_clamp_oom_score_adj(value)}\n")


# ════════════════════════════════════════════════════════════════════
# WorkerClient — single-worker lifecycle and operation dispatch. Log
# dispatch itself lives in nanopynix.logging.CallbackBus, shared with
# inproc.Session -- see that class's docstring for why the worker's own
# subscribe_logs is not built on it.
# ════════════════════════════════════════════════════════════════════


class WorkerClient:  # pyright: ignore[reportUnusedClass] -- imported by the public Session façade
    """Own one multiprocessing worker and dispatch its RPC operations.

    Provides:
    - ``call()`` — worker RPC dispatch without cross-service serialization.
    - ``subscribe()`` / ``log_stream()`` — log event access.
    - Direct access to ``store_stub`` and ``eval_stub`` for gRPC calls.
    """

    def __init__(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
        self,
        *,
        store_uri: str = DEFAULT_STORE_URI,
        nix_conf: Path | None = None,
        load_config: bool = True,
        settings: dict[str, str] | None = None,
        experimental_features: list[str] | None = None,
        verbosity: LogLevel | None = None,
        nix_path: Sequence[str] | None = None,
        primops: list[PrimOpSpec] | None = None,
        primop_callables: dict[str, Callable[..., Any]] | None = None,
        worker_oom_score_adj: int | None = None,
        rpc_timeout: float = DEFAULT_RPC_TIMEOUT_SECONDS,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        worker_preload: Sequence[str] = DEFAULT_WORKER_PRELOAD,
        namespace: OverlayNamespace | None = None,
    ) -> None:
        self._store_uri = store_uri
        self._nix_conf = nix_conf
        self._load_config = load_config
        self._settings = settings or {}
        self._features = experimental_features or []
        self._verbosity = verbosity
        self._nix_path = list(nix_path) if nix_path else []
        self._primops = primops or []
        self._primop_callables = primop_callables or {}
        self._worker_oom_score_adj = worker_oom_score_adj
        self._worker_preload = list(worker_preload)
        self._namespace = namespace
        self.rpc_timeout = rpc_timeout
        self._shutdown_timeout = shutdown_timeout
        self._worker_pid: int | None = None
        self._worker_proc: Any = None
        # How the worker went away, and whether anybody wanted it to. Read
        # once and cached, because the process object is dropped during
        # teardown and the forkserver only reports the status once.
        self._worker_exit_status: int | None = None
        self._unexpected_death: WorkerSignaledError | None = None
        self._expected_worker_death = False
        self._stopping_the_worker = False
        self._channel = None
        self._worker_service_stub: WorkerServiceStub | None = None
        self._store_service_stub: StoreServiceStub | None = None
        self._eval_service_stub: EvalServiceStub | None = None
        self._primop_handler: Any = None
        self._log_bus: CallbackBus = CallbackBus()
        self._log_task: asyncio.Task[None] | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._next_request_id = 1

    # ── lifecycle ──────────────────────────────────────────────────

    async def open(self) -> None:
        """Spawn the worker via multiprocessing forkserver and initialise Nix.

        A failure here closes what it already opened, and then raises. Nothing
        else can: ``Session.__aenter__`` is the only caller, and Python does
        not run ``__aexit__`` for a ``__aenter__`` that raised. Without this,
        a settings value Nix refuses left the channel open, the log task
        running, and the worker process alive -- an orphan holding a Nix store
        connection that nothing in this process remembered.
        """
        if self._namespace is not None:
            # Ask before spawning. The namespace is set up inside the
            # forkserver child, so a host that cannot do it kills the child
            # before it serves anything, and the caller would see a dead
            # worker rather than the reason it died.
            support = await anyio.to_thread.run_sync(
                functools.partial(probe_namespace_support, root=Path(self._namespace.upper_dir).parent)
            )
            if not support:
                raise RuntimeError(f"cannot start an overlay store worker: {support.reason}")

        stack = contextlib.AsyncExitStack()
        self._stack = stack
        try:
            await self._open_worker(stack)
        except BaseException:
            # BaseException, not Exception: a cancellation between the spawn
            # and the Init reply leaves the same orphan a refused setting
            # does. close() shields its own teardown, so it finishes either
            # way, and re-raising keeps the reason the caller needs.
            await self.close()
            raise

    async def _open_worker(self, stack: contextlib.AsyncExitStack) -> None:
        """Bring up the channel, the log task and Nix. ``open()`` owns the failure path.

        The stack is a parameter rather than ``self._stack``, which ``close()``
        clears: one caller, and it hands over the stack it is about to close.
        """
        self._primop_handler = ManagerPrimopServiceHandler()
        self._primop_handler.register_all(self._primop_callables)

        # A partial rather than a closure: grpclib_transports pickles the
        # factory through the forkserver, so it has to be a module-level
        # function plus picklable arguments. An environment variable would not
        # arrive at all -- the forkserver copies the environment once, when it
        # starts, and reuses that copy for every child.
        service_factory = (
            worker_service_factory
            if self._namespace is None
            else functools.partial(worker_service_factory, namespace=self._namespace)
        )

        self._channel = await stack.enter_async_context(
            multiprocessing_worker_with_backchannel(
                service_factory,
                [
                    self._primop_handler,
                ],
                on_process_start=self._on_worker_process_start,
                # What the child does after its transport closes. Nothing ran
                # there before, so a worker that the client never asked to
                # shut down politely -- a cancelled close, a `Shutdown` that
                # timed out, a parent that died -- left every open evaluator's
                # thread to exit still registered with the Boehm collector.
                # See `worker_child_teardown`. Pickled through the forkserver,
                # like the factory above.
                #
                # The cast is contravariance and nothing else: the transport
                # declares `Collection[IServable]`, and the worker annotates
                # the concrete handlers so that beartype can check them. See
                # that function's docstring.
                child_teardown=cast("ChildTeardown", worker_child_teardown),
                # One list for the whole process, whoever asks first. The
                # forkserver is a `multiprocessing` singleton, and it copies
                # its preload list, its environment and its `sys.path` when it
                # starts. See NanopynixSettings.worker_preload.
                preload=self._worker_preload,
                max_concurrency=DEFAULT_WORKER_MAX_CONCURRENCY,
                # Installs the codec on *both* ends: the channel yielded here,
                # and -- because grpclib_transports forwards it through the
                # forkserver process args -- the worker's own server. Both are
                # required; grpclib drops the trailer silently if either is
                # missing. See nanopynix.rpc._status_details.
                status_details_codec=NIX_STATUS_DETAILS_CODEC,
            ),
        )
        self._worker_service_stub = WorkerServiceStub(self._channel)
        self._store_service_stub = StoreServiceStub(self._channel)
        self._eval_service_stub = EvalServiceStub(self._channel)
        self._log_task = asyncio.create_task(self._forward_worker_logs(), name="nanopynix-worker-logs")

        proto_primops = [
            PrimOpSpecPB(
                name=p.name,
                arity=p.arity,
                args=list(p.args),
                doc=p.doc,
                import_path=p.import_path,
                rpc=p.rpc if hasattr(p, "rpc") else False,
            )
            for p in self._primops
        ]

        # Initialize Nix in the worker
        init_response = await self.invoke(
            self.worker_stub.init,
            InitRequest(
                store_uri=self._store_uri,
                nix_conf=str(self._nix_conf) if self._nix_conf is not None else None,
                load_config=self._load_config,
                settings=self._settings,
                experimental_features=self._features,
                primops=proto_primops,
                verbosity=self._verbosity,
                nix_path=self._nix_path,
            ),
            timeout=self.rpc_timeout,
        )
        if init_response.status != WORKER_INIT_STATUS_OK:
            raise RuntimeError(f"Worker init failed: {init_response.status}")

    async def close(self) -> None:
        """Ask the worker to shut down, then make sure it is gone.

        The polite half -- the ``Shutdown`` RPC -- is interruptible and may
        fail; the teardown half is not, and runs under a shield. A caller's
        deadline (``Session.close`` has one) or an outright cancellation can
        cut the request short, but it cannot leave a worker process behind:
        that would be a live Nix store connection nothing in this process
        remembers, reported as a clean shutdown.

        Everything inside the shield is separately bounded, so the shield
        cannot become a hang of its own.

        **This method records a worker that died on its own, and does not
        raise it.** ``Session.close`` reads :attr:`unexpected_death` and
        reports it there, because this runs from that method's ``finally``
        and an exception here would replace whatever sent it.
        """
        # Before anything asks the worker to stop, so this process's own
        # SIGTERM can never be read back as the crash. Non-blocking: a live
        # worker gives None and costs nothing.
        self._note_worker_death(self._read_worker_exit_status(self._worker_proc))
        try:
            if self._worker_service_stub is not None:
                try:
                    await self.invoke(self.worker_stub.shutdown, ShutdownRequest(), timeout=self._shutdown_timeout)
                except (
                    TimeoutError,
                    GRPCError,
                    StreamTerminatedError,
                    ConnectionError,
                    WorkerDiedError,
                ):
                    # A worker that will not answer Shutdown is still going to
                    # be stopped below, so none of these is worth raising.
                    # Cancellation used to be caught here too and it is not any
                    # more: swallowing it left the enclosing scope with nothing
                    # to notice, so Session.close's deadline expired without
                    # ever producing a TimeoutError. The teardown is shielded,
                    # so letting it through costs nothing and is the only way
                    # the caller hears about it.
                    #
                    # `invoke` already reaped and recorded, if what it caught
                    # was a death. That is what closes the one window the
                    # check above cannot see: a worker that was alive when
                    # this method began and aborted while serving Shutdown.
                    logger.debug("worker shutdown failed (expected during teardown)", exc_info=True)
        finally:
            # From here on the worker's exit status is this process's own
            # doing, whether the transport terminates it or this class kills
            # it. `_note_worker_death` reads the flag and stops recording, so
            # neither the rest of this close nor a second one can report our
            # SIGTERM as a crash.
            self._stopping_the_worker = True
            # One shield over the whole teardown, not just the part that stops
            # the process. Every await below is a place an outer cancellation
            # would otherwise re-fire and skip the rest -- and the rest is what
            # stops the worker. Bounded throughout, so shielding cannot turn
            # into a hang: at most _LOG_DRAIN_TIMEOUT_SECONDS +
            # _WORKER_TEARDOWN_TIMEOUT_SECONDS + two joins.
            with anyio.CancelScope(shield=True):
                await self._teardown()
                # Inside the `finally`, and not after the whole `try`: this is
                # what ends every `log_stream` iterator, and a teardown that
                # raised used to skip it and leave each one waiting for an
                # event that would never arrive. inproc's `close` says the
                # same thing in its own `finally`.
                self._log_bus.emit(None)

    async def _teardown(self) -> None:
        """Drain the log stream and dispose of the worker. Runs shielded."""
        if self._log_task is not None:
            # The worker ends the SubscribeLogs stream itself when it handles
            # Shutdown, so give the task a moment to finish on its own:
            # cancelling it instead resets a stream the worker is still
            # serving, which it logs as a traceback on the stderr this process
            # inherits. Cancellation stays as the fallback for a worker that
            # died or never got the request.
            with anyio.move_on_after(_LOG_DRAIN_TIMEOUT_SECONDS):
                await asyncio.shield(self._log_task)
            if not self._log_task.done():
                self._log_task.cancel()
                with contextlib.suppress(anyio.get_cancelled_exc_class()):
                    await self._log_task
            self._log_task = None
        # Back to exactly the state __init__ left, so a closed client is
        # indistinguishable from one that was never opened and both refuse work
        # the same way -- see invoke(). inproc's Session says the same thing
        # with one `_opened` flag; here the stubs *are* the flag.
        self._channel = None
        self._worker_service_stub = None
        self._store_service_stub = None
        self._eval_service_stub = None
        stack, self._stack = self._stack, None
        if stack is None:
            return
        # grpclib_transports' teardown closes the channel first and stops the
        # process second. Bounding the first half is what keeps the second
        # reachable; _stop_worker_process then finishes the job whichever way
        # the exit stack went, including having raised.
        try:
            with anyio.move_on_after(_WORKER_TEARDOWN_TIMEOUT_SECONDS):
                await stack.aclose()
        finally:
            await self._stop_worker_process()

    # ── operation dispatch ─────────────────────────────────────────

    async def invoke(self, method: Callable[..., Any], request: Any, *, timeout: float) -> Any:  # noqa: ASYNC109 -- timeout passed to grpclib stub method which accepts a timeout parameter
        """Assign a worker-local operation ID and dispatch one unary RPC."""
        if self._channel is None:
            # Backstop rather than the guard that normally fires: every caller
            # reaches a stub property first (`self.worker_stub.shutdown` is
            # evaluated before invoke runs), and those refuse a closed session
            # with this same class. This catches a method captured while the
            # session was open and called after it closed.
            raise SessionClosedError("rpc Session is not open — use async with")
        request_id = self._next_request_id
        self._next_request_id += 1
        request.request_id = request_id
        for capture in ACTIVE_LOG_CAPTURES.get():
            capture._register_request(request_id)  # type: ignore[reportPrivateUsage] -- the ACTIVE_LOG_CAPTURES dispatch contract; inproc does the same  # noqa: SLF001
        try:
            return await _grpc_call(method(request, timeout=timeout))
        except WorkerDiedError as exc:
            # The one seam that answers "how". `_grpc_call` knows the stream
            # broke and nothing else; only this class holds the process. It
            # stays module-private and single-subject, and this catches what
            # it raised.
            raise await self._worker_death_error(exc) from exc

    async def _worker_death_error(self, disconnect: WorkerDiedError) -> WorkerDiedError:
        """Name the signal that broke this call, or keep what the transport said.

        The upgrade is the whole point: an abort, a segmentation fault and an
        OOM kill are one closed pipe at this boundary, and the exit status is
        the only thing that tells them apart.
        """
        return self._note_worker_death(await self._reap_worker()) or disconnect

    # ── log access ─────────────────────────────────────────────────

    def log_stream(self) -> AsyncIterator[LogEvent]:
        """Async iterator over log events. inproc's ``log_stream`` is the same call.

        Bounded, and it discards the oldest event when the caller does not
        keep up. See :func:`nanopynix.logging.bus_log_stream`, which holds the
        one implementation both engines use.
        """
        return bus_log_stream(self._log_bus)

    def subscribe(self, callback: LogCallback) -> BusSubscription:
        """Subscribe a callback to all log events.

        The callback receives a :class:`~nanopynix.models.LogEvent`, and
        ``None`` once when the stream ends. The worker sends the bare proto
        over gRPC, and ``CallbackBus.emit`` normalises it -- see that
        docstring for why a subscriber never sees the wire type.
        """
        return self._log_bus.subscribe(callback)

    async def _forward_worker_logs(self) -> None:
        try:
            async for event in self.worker_stub.subscribe_logs(SubscribeLogsRequest()):
                self._log_bus.emit(event)
        except anyio.get_cancelled_exc_class():
            raise
        except (GRPCError, StreamTerminatedError, ConnectionError):
            logger.debug("worker log stream ended", exc_info=True)

    def _on_worker_process_start(self, proc: Any) -> None:
        # The process object, not just its pid: close()'s teardown needs to be
        # able to stop it directly if the exit stack that normally owns that
        # job does not get there. See _stop_worker_process.
        self._worker_pid = proc.pid
        self._worker_proc = proc
        self._set_worker_oom_score_adj(self._worker_oom_score_adj)

    # ── worker exit status ─────────────────────────────────────────
    #
    # **A worker that aborts used to be indistinguishable from one that
    # stopped**, because nothing here read the exit status the parent already
    # holds. The three methods below are the whole repair, and issue #55 is
    # the account of what it costs to be without them: a run of the suite
    # reported success while a worker core-dumped inside it.
    #
    # `multiprocessing` reports the status as a negative number for a signal,
    # because the forkserver passes the child's wait status through
    # `os.waitstatus_to_exitcode`.

    def _read_worker_exit_status(self, proc: Any) -> int | None:
        """Cache and return the exit status, without waiting for it.

        ``None`` means the worker is still running, or that the forkserver has
        not reported yet. Both are indistinguishable here and neither is worth
        waiting for at a point where waiting is not allowed -- see
        :meth:`_reap_worker` for the point where it is.
        """
        if self._worker_exit_status is None and proc is not None:
            self._worker_exit_status = proc.exitcode
        return self._worker_exit_status

    async def _reap_worker(self) -> int | None:
        """Wait a bounded time for the exit status of a worker that just died.

        Shielded, because every caller runs while an exception is already on
        its way out of a scope that may be about to be cancelled. An
        unshielded await there would raise the cancellation instead, and the
        caller would never hear how its worker died.
        """
        if self._worker_exit_status is not None:
            return self._worker_exit_status
        proc = self._worker_proc
        if proc is None:
            return None
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(proc.join, _WORKER_REAP_TIMEOUT_SECONDS)
        return self._read_worker_exit_status(proc)

    def _note_worker_death(self, status: int | None) -> WorkerSignaledError | None:
        """Record a death by signal, and return the exception that names it.

        A positive status is an exit and not a crash, and ``None`` is a worker
        that is still running or has not been reported yet. Neither is a
        signal, so neither produces this class.

        **Nothing is recorded once teardown has begun, and a measurement is
        what put that rule here.** The transport stops a worker that did not
        exit on its own with ``SIGTERM``, so a session holding an open store
        at close ends at status ``-15`` on the ordinary path. The status is
        cached, a second ``close()`` read it back, and 99 LSP tests then
        failed with "this process did not signal it" about a signal this
        process had sent. See ``close()``, which sets the flag.
        """
        if self._stopping_the_worker or status is None or status >= 0:
            return None
        error = WorkerSignaledError(
            f"rpc worker {self._worker_pid} was killed by {signal_name(-status)} (exit status {status}); "
            "this process did not signal it",
            exit_status=status,
        )
        if self._unexpected_death is None:
            self._unexpected_death = error
        return error

    @property
    def unexpected_death(self) -> WorkerSignaledError | None:
        """The signal that killed this worker, when nothing asked it to stop.

        ``Session.close`` reads this and reports it, because the three places
        that suppress a ``WorkerDiedError`` during teardown are each right to
        suppress it and together mean nobody hears about a crash.

        ``None`` when the worker is alive, when it exited normally, or when a
        caller declared the death expected -- see
        ``_expected_worker_death``, which is how a test that kills its own
        worker says that it meant to.
        """
        if self._expected_worker_death:
            return None
        return self._unexpected_death

    async def _stop_worker_process(self) -> None:
        """Terminate, then kill, the worker if it is somehow still running.

        A no-op on every normal close: the exit stack has already stopped the
        process by the time this runs. It exists for the case where it did not
        -- see close()'s teardown block for how that happens -- because a
        surviving worker holds a Nix store connection, and nothing else in this
        process would ever come back for it.
        """
        proc = self._worker_proc
        self._worker_proc = None
        if proc is None:
            return
        try:
            if not proc.is_alive():
                return
            logger.warning("nanopynix: worker %s outlived its teardown; stopping it", proc.pid)
            proc.terminate()
            await anyio.to_thread.run_sync(proc.join, _WORKER_JOIN_TIMEOUT_SECONDS)
            if proc.is_alive():
                proc.kill()
                await anyio.to_thread.run_sync(proc.join, _WORKER_JOIN_TIMEOUT_SECONDS)
        finally:
            # The last moment the process object exists. Whatever it says now
            # is all this client will ever know, so cache it -- including for
            # the terminate above, whose SIGTERM is this process's own doing
            # and is what `_read_worker_exit_status` must not mistake for a
            # crash. It cannot: the death was already recorded before teardown
            # began. See close().
            self._read_worker_exit_status(proc)

    def _set_worker_oom_score_adj(self, value: int | None) -> None:
        if value is None:
            return
        pid = self._worker_pid
        if pid is None:
            return
        try:
            _write_oom_score_adj(pid, value)
        except OSError:
            logger.debug("failed to set worker oom_score_adj", exc_info=True)

    async def get_verbosity(self) -> LogLevel:
        """Return the current worker-side Nix log verbosity."""
        response = await self.invoke(self.worker_stub.get_verbosity, GetVerbosityRequest(), timeout=self.rpc_timeout)
        self._verbosity = response.verbosity
        return response.verbosity

    async def set_verbosity(self, verbosity: LogLevel) -> LogLevel:
        """Set the worker-side Nix log verbosity."""
        response = await self.invoke(
            self.worker_stub.set_verbosity, SetVerbosityRequest(verbosity=verbosity), timeout=self.rpc_timeout
        )
        self._verbosity = response.verbosity
        return response.verbosity

    async def get_settings(self, *, overridden_only: bool = False) -> GetSettingsResponse:
        """Return the worker's effective Nix settings, and their provenance."""
        return await self.invoke(
            self.worker_stub.get_settings,
            GetSettingsRequest(overridden_only=overridden_only),
            timeout=self.rpc_timeout,
        )

    async def set_settings(self, settings: Mapping[str, str]) -> dict[str, str]:
        """Apply global Nix settings in the worker, and return each value read back."""
        response = await self.invoke(
            self.worker_stub.set_settings,
            SetSettingsRequest(settings=dict(settings)),
            timeout=self.rpc_timeout,
        )
        return dict(response.applied)

    # The three stub properties are where a closed session is actually refused:
    # every dispatch names one, so together they are rpc's counterpart of
    # inproc's `Session.run` calling `_check_open`. They used to raise
    # WorkerDiedError("Worker not started"), which was wrong twice over --
    # nothing died, and the case it really covered ("never opened") had no way
    # to also mean "closed", because close() left the stubs in place. close()
    # now clears them, so both situations arrive as one class, as inproc has
    # always had it. A worker that genuinely dies mid-session still surfaces as
    # WorkerDiedError, from _grpc_call.
    #
    # Neither engine guards store()/eval()/repl() themselves: they hand back an
    # object, and the object's first piece of work is what fails.
    @property
    def worker_stub(self) -> WorkerServiceStub:
        stub = self._worker_service_stub
        if stub is None:
            raise SessionClosedError("rpc Session is not open — use async with")
        return stub

    @property
    def store_stub(self) -> StoreServiceStub:
        stub = self._store_service_stub
        if stub is None:
            raise SessionClosedError("rpc Session is not open — use async with")
        return stub

    @property
    def eval_stub(self) -> EvalServiceStub:
        stub = self._eval_service_stub
        if stub is None:
            raise SessionClosedError("rpc Session is not open — use async with")
        return stub
