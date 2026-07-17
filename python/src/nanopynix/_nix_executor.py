"""Asynchronous execution for Nix C++ objects."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")


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
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
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

    def _initialize_thread(self) -> None:
        initializer = self._thread_initializer
        if initializer is None:
            return
        initializer()
        self._thread_started.set()

    async def run(self, func: Callable[..., _T], *args: Any) -> _T:
        return await self._submit(func, args, allow_when_closing=False)

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
            future = self._pool.submit(func, *args)
            self._futures.add(future)
        future.add_done_callback(self._discard_future)
        loop = asyncio.get_running_loop()
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

    async def drain(self, *, timeout: float | None = None) -> None:
        """Wait until all submitted work has finished without cancelling it."""
        with self._lock:
            futures = tuple(future for future in self._futures if not future.done())
        if not futures:
            return
        wrapped = [asyncio.wrap_future(future) for future in futures]
        _, pending = await asyncio.wait(wrapped, timeout=timeout)
        if pending:
            raise TimeoutError("timed out waiting for Nix executor work to finish")

    def shutdown(self, wait: bool = True) -> None:
        self.begin_close()
        finalizer = self._thread_finalizer
        if finalizer is not None and self._thread_started.is_set():
            # Evaluator executors have one worker. Drain before shutdown, then
            # execute the matching GC teardown on that same owning thread.
            self._pool.submit(finalizer).result()
        self._pool.shutdown(wait=wait)
        self._closed = True

    @property
    def closed(self) -> bool:
        """Return whether this executor has been shut down."""
        return self._closed
