"""Asynchronous execution for Nix C++ objects."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import TYPE_CHECKING, Any, TypeVar

import anyio

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

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


class NixThreadExecutor:
    """Run Nix work on a bounded pool of dedicated threads.

    Evaluator callers use one worker to preserve ``EvalState`` affinity. Store
    callers may use several workers because Nix stores are thread-safe.
    """

    def __init__(
        self,
        *,
        max_workers: int = 1,
        thread_name_prefix: str = "nix",
        thread_initializer: Callable[[], None] | None = None,
        thread_finalizer: Callable[[], None] | None = None,
        stack_size: int | None = None,
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
        """
        with self._lock:
            if not self._accepting:
                raise RuntimeError("Nix executor is closing")
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
        with self._lock:
            if not self._accepting and not allow_when_closing:
                raise RuntimeError("Nix executor is closing")
            self._ensure_worker_spawned()
            future = self._pool.submit(func, *args)
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
        return await asyncio.wrap_future(future, loop=loop)

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
        """Resume accepting work after a timed-out non-destructive close."""
        with self._lock:
            if not self._closed:
                self._accepting = True

    def has_pending_work(self) -> bool:
        with self._lock:
            return any(not future.done() for future in self._futures)

    async def drain(self, *, timeout: float | None = None) -> None:  # noqa: ASYNC109 -- timeout passed to anyio.move_on_after which accepts a timeout parameter
        """Wait until all submitted work has finished without cancelling it."""
        with self._lock:
            futures = tuple(future for future in self._futures if not future.done())
        if not futures:
            return
        loop = asyncio.get_running_loop()
        with anyio.move_on_after(timeout) as scope:
            for future in futures:
                await asyncio.wrap_future(future, loop=loop)
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
        self.begin_close()
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
