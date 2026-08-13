"""Direct unit tests for NixThreadExecutor's lifecycle edge cases.

Real Nix eval/store tests only ever exercise the executor's happy path (submit
work, shut down once). These "dumb coverage" tests pin down the guard clauses
around closing, draining, and idempotent shutdown directly, since nothing
else constructs the failure conditions (a busy pool being closed, a timed-out
drain, a duplicate shutdown) needed to reach them.
"""

from __future__ import annotations

import asyncio
import threading
import time

import anyio
import pytest

from nanopynix._core._nix_executor import (
    NixThreadExecutor,  # pyright: ignore[reportPrivateUsage] -- test reaches into the private executor module for the lifecycle edge cases described above
)
from nanopynix.exceptions import EvaluatorAbandonedError


def test_rejects_a_non_positive_max_workers() -> None:
    with pytest.raises(ValueError, match="max_workers must be at least 1"):
        NixThreadExecutor(max_workers=0)


async def test_thread_initializer_runs_on_the_worker_thread() -> None:
    executor = NixThreadExecutor(thread_initializer=lambda: None)
    try:
        await executor.run(lambda: None)
        assert executor._thread_started.is_set()  # pyright: ignore[reportPrivateUsage] -- asserts the executor's private flag directly to verify the initializer ran on the worker thread
    finally:
        executor.shutdown()


async def test_run_rejects_new_work_after_begin_close() -> None:
    executor = NixThreadExecutor()
    try:
        executor.begin_close()
        with pytest.raises(RuntimeError, match="Nix executor is closing"):
            await executor.run(lambda: None)
    finally:
        executor.resume()
        executor.shutdown()


def test_run_sync_rejects_new_work_after_begin_close() -> None:
    executor = NixThreadExecutor()
    try:
        executor.begin_close()
        with pytest.raises(RuntimeError, match="Nix executor is closing"):
            executor.run_sync(lambda: None)
    finally:
        executor.resume()
        executor.shutdown()


async def test_begin_close_with_force_cancels_pending_work() -> None:
    executor = NixThreadExecutor(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def _blocker() -> None:
        started.set()
        release.wait(timeout=5)

    blocking = asyncio.ensure_future(executor.run(_blocker))
    try:
        await loop.run_in_executor(None, started.wait, 5)
        never_started = asyncio.ensure_future(executor.run(lambda: None))
        executor.begin_close(force=True)

        with pytest.raises(Exception):  # noqa: PT011, B017 -- either CancelledError or the future never running is acceptable
            await never_started
    finally:
        release.set()
        await blocking
        executor.resume()
        executor.shutdown()


async def test_resume_allows_accepting_work_again() -> None:
    executor = NixThreadExecutor()
    try:
        executor.begin_close()
        executor.resume()
        assert await executor.run(lambda: 1) == 1
    finally:
        executor.shutdown()


async def test_drain_returns_immediately_with_no_pending_work() -> None:
    executor = NixThreadExecutor()
    try:
        await executor.drain()
    finally:
        executor.shutdown()


async def test_drain_raises_timeout_error_when_work_outlasts_the_deadline() -> None:
    executor = NixThreadExecutor(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def _blocker() -> None:
        started.set()
        release.wait(timeout=5)

    blocking = asyncio.ensure_future(executor.run(_blocker))
    try:
        await loop.run_in_executor(None, started.wait, 5)
        with pytest.raises(TimeoutError, match="timed out waiting for Nix executor work"):
            await executor.drain(timeout=0.01)
    finally:
        release.set()
        await blocking
        executor.shutdown()


async def test_drain_waits_for_pending_work_to_finish() -> None:
    executor = NixThreadExecutor(max_workers=1)
    try:
        await executor.run(time.sleep, 0.05)
        await executor.drain(timeout=5.0)
        assert not executor.has_pending_work()
    finally:
        executor.shutdown()


def test_shutdown_is_idempotent() -> None:
    executor = NixThreadExecutor()
    executor.shutdown()
    executor.shutdown()  # must not raise or re-run the finalizer
    assert executor.closed


# --- Cancellation and poisoning (#37) ---------------------------------------
#
# These use a plain blocking Python callable rather than Nix. What is under
# test is the executor's own bookkeeping around a cancel: whether it waits, how
# long, and what it does when the wait runs out. Nix decides whether the work
# really stops, and the tests in nanopynix/tests/inproc/test_inproc_cancel.py
# cover that half.


async def test_a_cancelled_call_that_stops_in_time_leaves_the_executor_healthy() -> None:
    executor = NixThreadExecutor(max_workers=1, interrupt_grace=5.0)
    release = threading.Event()

    # Stands in for Nix reaching its next checkInterrupt(). It has to fire from
    # outside this task: the cancelled caller does not come back from
    # move_on_after until _interrupt's bounded wait is over, so releasing the
    # work after that line would always be too late.
    timer = threading.Timer(0.4, release.set)
    timer.start()

    try:
        with anyio.move_on_after(0.2):
            await executor.run(release.wait, 10)

        assert executor.poisoned is None
        assert not executor.has_pending_work()
        assert await executor.run(lambda: 2 + 2) == 4
    finally:
        timer.cancel()
        release.set()
        executor.shutdown()


async def test_a_cancelled_call_that_will_not_stop_poisons_the_executor() -> None:
    executor = NixThreadExecutor(max_workers=1, interrupt_grace=0.1)
    release = threading.Event()

    def _blocker() -> None:
        release.wait(timeout=10)

    try:
        with anyio.move_on_after(0.2):
            await executor.run(_blocker)

        assert executor.poisoned is not None
        assert "did not answer an interrupt" in executor.poisoned
    finally:
        release.set()
        executor.shutdown()


async def test_a_poisoned_executor_refuses_every_later_call() -> None:
    executor = NixThreadExecutor(max_workers=1, interrupt_grace=0.1)
    release = threading.Event()

    try:
        with anyio.move_on_after(0.2):
            await executor.run(release.wait, 10)
        assert executor.poisoned is not None

        with pytest.raises(EvaluatorAbandonedError):
            await executor.run(lambda: None)
        with pytest.raises(EvaluatorAbandonedError):
            await executor.run_closing(lambda: None)
        with pytest.raises(EvaluatorAbandonedError):
            executor.run_sync(lambda: None)
    finally:
        release.set()
        executor.shutdown()


async def test_a_poisoned_executor_never_resumes() -> None:
    executor = NixThreadExecutor(max_workers=1, interrupt_grace=0.1)
    release = threading.Event()

    try:
        with anyio.move_on_after(0.2):
            await executor.run(release.wait, 10)
        assert executor.poisoned is not None

        executor.resume()

        with pytest.raises(EvaluatorAbandonedError):
            await executor.run(lambda: None)
    finally:
        release.set()
        executor.shutdown()


async def test_drain_returns_at_once_on_a_poisoned_executor() -> None:
    executor = NixThreadExecutor(max_workers=1, interrupt_grace=0.1)
    release = threading.Event()

    try:
        with anyio.move_on_after(0.2):
            await executor.run(release.wait, 10)
        assert executor.poisoned is not None

        # The abandoned work is still running. With timeout=None the old
        # behaviour would wait for it for ever.
        started = time.monotonic()
        await executor.drain()
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        executor.shutdown()


async def test_a_poisoned_shutdown_returns_while_the_thread_is_still_busy() -> None:
    """And it queues the finalizer rather than skipping it.

    Skipping it would let the thread reach the pool's stop marker and exit
    while Boehm GC still lists it. A later collection then signals a dead
    thread and aborts the process. The finalizer must run before the thread
    ends, however long that takes.
    """
    finalized = threading.Event()
    release = threading.Event()
    executor = NixThreadExecutor(
        max_workers=1,
        interrupt_grace=0.1,
        thread_initializer=lambda: None,
        thread_finalizer=finalized.set,
    )

    try:
        with anyio.move_on_after(0.2):
            await executor.run(release.wait, 10)
        assert executor.poisoned is not None

        started = time.monotonic()
        executor.shutdown(wait=True)
        assert time.monotonic() - started < 1.0
        assert executor.closed
        assert not finalized.is_set()  # still queued behind the busy thread

        release.set()
        with anyio.fail_after(10):
            while not finalized.is_set():  # noqa: ASYNC110 -- a threading.Event has no async wait
                await anyio.sleep(0.05)
    finally:
        release.set()
