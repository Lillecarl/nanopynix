"""Asynchronous execution for Nix C++ objects."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import weakref
from typing import TYPE_CHECKING, Any, TypeVar

import anyio
from nanopynix_bindings import signals as nanopynix_signals

from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import EvaluatorAbandonedError

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

# How long a cancelled operation gets to answer its interrupt before the
# executor is abandoned. Nix polls `checkInterrupt()` frequently where it polls
# at all, so a response is either prompt or never: this bounds the "never" case
# and is not a budget the interruptible path is expected to use.
DEFAULT_INTERRUPT_GRACE_SECONDS = 2.0

# Every executor abandoned in this process. Weak, because an abandoned executor
# is otherwise ordinary garbage; its *thread* is what leaks, not the object.
_ABANDONED: weakref.WeakSet[NixThreadExecutor] = weakref.WeakSet()


def abandoned_work_is_running() -> bool:
    """Whether an abandoned executor still has a thread inside Nix.

    True means this process cannot end tidily. The thread keeps allocating from
    the Boehm collector, and Boehm stops the world by signalling every
    registered thread and waiting about 0.45s in total for all of them. Under
    the extra heap a machine can miss that budget, and Boehm then aborts with
    "Signals delivery fails constantly". Python's finalization is the worst
    moment for it, but not the only one.

    A process that owns its own life may act on this -- the RPC worker calls
    ``os._exit`` rather than leave a core dump behind. nanopynix does **not**
    act on it inside a host application: exiting someone else's process is not
    a library's decision to make.
    """
    return any(executor.has_pending_work() for executor in _ABANDONED)


# Nix's own evaluator stack size, in bytes: `60 * 1024 * 1024`, straight from
# `nix::setStackSize(60 * 1024 * 1024)` in Nix's `src/nix/main.cc`. (60 rather
# than 64 because macOS on GitHub Actions has a hard limit slightly under
# 64 MiB, per the comment there.) Nix has no `stack-size` setting to read it
# from, so it is a constant here too.
#
# Note *where* Nix calls it: the CLI's `main()`, not `initNix()`, so no
# embedder of libnix inherits it however it initialises -- which is the whole
# bug. Nix's
# default `max-call-depth` of 10000 needs roughly 27 MB of C stack, so on the
# 8 MiB a thread inherits from `RLIMIT_STACK` the stack is exhausted long
# before the counter fires and `let f = n: f (n + 1); in f 0` segfaults
# instead of raising.
#
# We apply it per-thread rather than by calling `nix::setStackSize`, which
# raises `RLIMIT_STACK` and so cannot exceed the *hard* limit -- 8 MiB on a
# stock host, where Nix itself warns ("Stack size hard limit is 8388608, which
# is less than the desired 62914560") and falls back to its SIGSEGV handler. A
# pthread stack is mmap'd and not bound by `RLIMIT_STACK`, so this succeeds
# where Nix's own mechanism does not.
NIX_EVALUATOR_STACK_SIZE = 62914560

# `threading.stack_size()` is process-global: it must be held only across the
# thread creation it is meant for, and always restored, or threads the host
# application creates afterwards silently inherit a 60 MiB reservation.
_STACK_SIZE_LOCK = threading.Lock()


def _noop() -> None:
    """Warm-up body: exists only so the pool has a reason to spawn its thread."""


def _run_interruptible[T](
    token: nanopynix_signals.InterruptToken,
    func: Callable[..., T],
    args: tuple[Any, ...],
) -> T:
    """Run ``func`` with ``token`` armed on this thread.

    The scope has to be entered *here* and not by the submitter, because Nix
    reads its interrupt predicate from a C++ ``thread_local``. Only code running
    on the Nix thread can install one for that thread.
    """
    with nanopynix_signals.interrupt_scope(token):
        return func(*args)


class NixThreadExecutor:
    """Run Nix work on a bounded pool of dedicated threads.

    Evaluator callers use one worker to preserve ``EvalState`` affinity. Store
    callers may use several workers because Nix stores are thread-safe.
    """

    def __init__(  # noqa: PLR0913 -- six independent thread properties; bundling them into a config object would only move the list
        self,
        *,
        max_workers: int = 1,
        thread_name_prefix: str = "nix",
        thread_initializer: Callable[[], None] | None = None,
        thread_finalizer: Callable[[], None] | None = None,
        stack_size: int | None = None,
        interrupt_grace: float | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if stack_size is not None and max_workers != 1:
            # Sizing more than one thread means holding the process-global
            # setting across several spawns, which needs a barrier to keep them
            # concurrent. No caller wants it -- Nix stores do not recurse, so
            # only the single-threaded evaluators ask for a stack at all.
            raise ValueError("stack_size is only supported with max_workers=1")
        self._stack_size = stack_size
        self._worker_spawned = False
        self._thread_initializer = thread_initializer
        self._thread_finalizer = thread_finalizer
        self._thread_started = threading.Event()
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
            initializer=self._initialize_thread if thread_initializer is not None else None,
        )
        self._lock = threading.Lock()
        self._futures: set[concurrent.futures.Future[Any]] = set()
        self._accepting = True
        self._closed = False
        self._shutdown_started = False
        # Resolved here rather than as a default argument value, which Python
        # binds once at definition time. Reading the module constant at
        # construction is what lets a caller -- and a test -- change it.
        self._interrupt_grace = DEFAULT_INTERRUPT_GRACE_SECONDS if interrupt_grace is None else interrupt_grace
        # Set once, and never cleared: the thread is gone for the life of the
        # process. Reading it is the only way a later caller learns that this
        # executor's thread is occupied by work that will not stop.
        self._poisoned: str | None = None

    def _ensure_worker_spawned(self) -> None:
        """Create this executor's thread with ``stack_size`` in force, once.

        ``ThreadPoolExecutor`` spawns lazily inside ``submit()``, so the stack
        size that takes effect is whatever is set at the *first submit* -- not
        at construction. A warm-up task run to completion pins it down: when it
        returns, the thread exists and its stack is already the right size.

        Degrades rather than fails. A host that has made
        ``threading.stack_size()`` unusable gets the old, too-small stack and a
        warning, not a Session that refuses to open.

        The caller must hold ``self._lock``.
        """
        size = self._stack_size
        if size is None or self._worker_spawned:
            return
        self._worker_spawned = True
        with _STACK_SIZE_LOCK:
            previous = threading.stack_size()
            # A floor, not an override: `nix::setStackSize` only ever raises
            # (`if (limit.rlim_cur < stackSize)`), so a host that has asked for
            # a bigger stack -- to run a raised `max-call-depth`, say -- keeps
            # it. `threading.stack_size()` returns 0 for "the system default",
            # which is the 8 MiB case this exists to replace.
            size = max(size, previous)
            try:
                threading.stack_size(size)
            except (ValueError, RuntimeError):
                logger.warning(
                    "could not request a %d-byte stack for the Nix evaluator thread; "
                    "deep recursion may exhaust the stack before Nix's max-call-depth "
                    "limit reports it",
                    size,
                    exc_info=True,
                )
                return
            try:
                self._pool.submit(_noop).result()
            finally:
                threading.stack_size(previous)

    def _initialize_thread(self) -> None:
        initializer = self._thread_initializer
        if initializer is None:
            return
        initializer()
        self._thread_started.set()

    async def run(self, func: Callable[..., _T], *args: Any) -> _T:
        return await self._submit(func, args, allow_when_closing=False)

    def run_sync(self, func: Callable[..., _T], *args: Any) -> _T:
        """Run ``func`` on this executor's thread and block until it finishes.

        For use from a worker thread that has no running event loop (e.g. one
        ``NixThreadExecutor``'s thread orchestrating a call into another's).

        No interrupt token, unlike :meth:`run`. A blocked caller has no way to
        set one, so arming a scope here would only cost a thread-local write
        that nothing can ever read.
        """
        with self._lock:
            self._require_usable(allow_when_closing=False)
            self._ensure_worker_spawned()
            future = self._pool.submit(func, *args)
            self._futures.add(future)
        future.add_done_callback(self._discard_future)
        return future.result()

    async def run_closing(self, func: Callable[..., _T], *args: Any) -> _T:
        """Run internal teardown work after :meth:`begin_close`."""
        return await self._submit(func, args, allow_when_closing=True)

    async def _submit(
        self,
        func: Callable[..., _T],
        args: tuple[Any, ...],
        *,
        allow_when_closing: bool,
    ) -> _T:
        token = nanopynix_signals.InterruptToken()
        with self._lock:
            self._require_usable(allow_when_closing=allow_when_closing)
            self._ensure_worker_spawned()
            future = self._pool.submit(_run_interruptible, token, func, args)
            self._futures.add(future)
        future.add_done_callback(self._discard_future)
        loop = asyncio.get_running_loop()
        # Deliberately asyncio-native, not anyio.to_thread: this future is
        # already running on a dedicated, independently managed thread (this
        # executor's own pool), not one anyio would spawn. Routing the wait
        # through anyio.to_thread.run_sync(future.result) would spend a slot
        # in anyio's shared, process-wide to_thread capacity limiter just to
        # block on a result that's already being computed elsewhere -- a
        # thread blocking on a thread, contending for a resource for no
        # benefit. asyncio.wrap_future costs nothing extra (an
        # add_done_callback resolving a future on the running loop) and this
        # project always runs anyio's asyncio backend, where asyncio-native
        # interop is explicitly supported.
        try:
            return await asyncio.wrap_future(future, loop=loop)  # noqa: TID251 -- the work already runs on this executor's own thread; see the comment above
        except asyncio.CancelledError:
            await self._interrupt(token, future, loop)
            raise

    async def _interrupt(
        self,
        token: nanopynix_signals.InterruptToken,
        future: concurrent.futures.Future[Any],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Ask Nix to stop, and wait a bounded time for it to really stop.

        ``concurrent.futures.Future.cancel()`` does nothing once the work has
        started, so without this the thread runs on and the caller only *looks*
        cancelled. An ``EvalState`` belongs to one thread, so the next call on
        this executor would queue behind work nobody wants.

        Shielded, because this runs while the caller is already being cancelled
        and an unshielded await would return at once. Bounded, because Nix
        answers only where it polls ``checkInterrupt()``: a pure evaluation
        never does, and an unbounded wait would trade an abandoned thread for a
        cancellation that hangs.
        """
        token.cancel()
        with anyio.CancelScope(shield=True), anyio.move_on_after(self._interrupt_grace) as scope:
            try:
                await asyncio.wrap_future(future, loop=loop)  # noqa: TID251 -- same dedicated-executor-thread interop as _submit above
            except asyncio.CancelledError:
                # move_on_after's own deadline. It must reach the scope.
                raise
            except BaseException:
                # Whatever the interrupted work raised, including the
                # OperationCancelled this interrupt just caused. Retrieving it
                # is the point: an unread future exception is reported by
                # asyncio at collection as "exception was never retrieved".
                # Debug rather than warning: no caller is waiting for this
                # outcome, and the cancellation itself is not a fault.
                logger.debug("cancelled Nix work finished with an exception", exc_info=True)
        if scope.cancelled_caught:
            self._poison(f"a Nix operation did not answer an interrupt within {self._interrupt_grace}s")

    def _require_usable(self, *, allow_when_closing: bool) -> None:
        """Reject work this executor cannot run. The caller must hold ``self._lock``.

        Poisoned is checked first, and ignores ``allow_when_closing``. Teardown
        work is exactly what must not be submitted to an abandoned thread:
        without this, ``run_closing`` would queue a finalizer behind an
        operation that never ends.
        """
        if self._poisoned is not None:
            raise EvaluatorAbandonedError(self._poisoned)
        if not self._accepting and not allow_when_closing:
            raise RuntimeError("Nix executor is closing")

    def _poison(self, reason: str) -> None:
        """Abandon this executor's thread, and refuse every later call.

        Not idempotent by accident: the first reason is the true one, and a
        second cancellation racing behind the first must not overwrite it.
        """
        with self._lock:
            if self._poisoned is not None:
                return
            self._poisoned = reason
            self._accepting = False
        _ABANDONED.add(self)
        logger.warning(
            "%s; abandoning the Nix thread. It keeps its %d-byte stack until the "
            "process ends. Nix stops only where it polls checkInterrupt(), and "
            "a pure evaluation never does.",
            reason,
            self._stack_size or 0,
        )

    @property
    def poisoned(self) -> str | None:
        """Why this executor was abandoned, or ``None`` while it is healthy."""
        return self._poisoned

    def _discard_future(self, future: concurrent.futures.Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)

    def begin_close(self, *, force: bool = False) -> None:
        """Reject new work and optionally cancel work that has not started."""
        with self._lock:
            self._accepting = False
            futures = tuple(self._futures)
        if force:
            for future in futures:
                future.cancel()

    def resume(self) -> None:
        """Resume accepting work after a timed-out non-destructive close.

        A poisoned executor never resumes. Its thread is still inside the
        operation that would not stop.
        """
        with self._lock:
            if not self._closed and self._poisoned is None:
                self._accepting = True

    def has_pending_work(self) -> bool:
        with self._lock:
            return any(not future.done() for future in self._futures)

    async def drain(self, *, timeout: float | None = None) -> None:  # noqa: ASYNC109 -- timeout passed to anyio.move_on_after which accepts a timeout parameter
        """Wait until all submitted work has finished without cancelling it.

        Returns at once on a poisoned executor. The outstanding work there is
        the operation that refused to stop, so waiting for it is exactly the
        hang this change removes -- and with ``timeout=None`` it would never
        end.
        """
        with self._lock:
            if self._poisoned is not None:
                return
            futures = tuple(future for future in self._futures if not future.done())
        if not futures:
            return
        loop = asyncio.get_running_loop()
        with anyio.move_on_after(timeout) as scope:
            for future in futures:
                await asyncio.wrap_future(future, loop=loop)  # noqa: TID251 -- same dedicated-executor-thread interop as _await_future above
        if scope.cancelled_caught:
            raise TimeoutError("timed out waiting for Nix executor work to finish")

    def shutdown(self, wait: bool = True) -> None:
        """Idempotent: a second call while shutdown is in progress or done is a no-op.

        A duplicate invocation (e.g. two racing close() calls on the owning
        EvalSession) must not submit the thread finalizer twice -- the second
        run would find the thread already unregistered from Boehm GC and
        raise, and without this guard that exception would propagate out of
        shutdown() before self._pool.shutdown() ever ran, leaking the
        underlying OS thread as still-registered-but-never-torn-down.
        """
        with self._lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            poisoned = self._poisoned
        self.begin_close()
        if poisoned is not None:
            # The thread is inside an operation that never answered its
            # interrupt, so this call must not *wait* for it. It must still
            # *queue* the finalizer behind it. `submit()` does not block; only
            # `.result()` does, and only the healthy path below calls that.
            #
            # Skipping the finalizer here is not an option, even though nothing
            # can say when it runs. `ThreadPoolExecutor.shutdown` puts its stop
            # marker in the same queue, so the worker would read that marker as
            # soon as the runaway operation ended and exit *still registered
            # with Boehm GC*. A later, unrelated collection then calls
            # pthread_kill on a dead tid and aborts the process with
            # "pthread_kill failed at suspend" -- the same defect that
            # `EvalSession.open` guards against, and a far worse outcome than
            # the leak. Ordering the finalizer ahead of the marker keeps the
            # thread's Boehm registration and its life the same length.
            #
            # An operation that never ends at all leaves both in place for
            # good. `abandoned_work_is_running()` reports that case.
            finalizer = self._thread_finalizer
            if finalizer is not None and self._thread_started.is_set():
                self._pool.submit(finalizer)
            logger.warning(
                "closing an abandoned Nix executor: %s. Its thread keeps running, and "
                "the thread finalizer is queued behind the operation that would not stop.",
                poisoned,
            )
            self._pool.shutdown(wait=False)
            self._closed = True
            return
        try:
            finalizer = self._thread_finalizer
            if finalizer is not None and self._thread_started.is_set():
                # Evaluator executors have one worker. Drain before shutdown, then
                # execute the matching GC teardown on that same owning thread.
                self._pool.submit(finalizer).result()
        finally:
            self._pool.shutdown(wait=wait)
            self._closed = True

    @property
    def closed(self) -> bool:
        """Return whether this executor has been shut down."""
        return self._closed
