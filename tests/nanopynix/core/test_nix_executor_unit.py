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

import pytest

from nanopynix._core._nix_executor import NixThreadExecutor  # pyright: ignore[reportPrivateUsage]


def test_rejects_a_non_positive_max_workers() -> None:
    with pytest.raises(ValueError, match="max_workers must be at least 1"):
        NixThreadExecutor(max_workers=0)


async def test_thread_initializer_runs_on_the_worker_thread() -> None:
    executor = NixThreadExecutor(thread_initializer=lambda: None)
    try:
        await executor.run(lambda: None)
        assert executor._thread_started.is_set()  # pyright: ignore[reportPrivateUsage]
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
        await executor.drain(timeout=5)
        assert not executor.has_pending_work()
    finally:
        executor.shutdown()


def test_shutdown_is_idempotent() -> None:
    executor = NixThreadExecutor()
    executor.shutdown()
    executor.shutdown()  # must not raise or re-run the finalizer
    assert executor.closed
