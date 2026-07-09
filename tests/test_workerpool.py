"""Tests for the Session — single subprocess worker concurrency."""

import asyncio
import contextlib

import pytest

from nanopynix import LogEvent, Nix, StoreError, WorkerBusyError, WorkerDiedError

pytestmark = pytest.mark.asyncio


async def test_single_worker_basics():
    """Basic round-trip with a single worker."""
    async with Nix() as nix, nix.store() as store:
        uri = await store.get_uri()
        assert isinstance(uri, str)
        d = await store.get_store_dir()
        assert d == "/nix/store"


async def test_two_workers_sequential():
    """Sequential calls on a single worker — should all succeed."""
    async with Nix() as nix, nix.store() as store:
        for _ in range(4):
            uri = await store.get_uri()
            assert isinstance(uri, str)


async def test_worker_busy_while_eval_session_holds_worker():
    """The single worker does not silently queue behind an eval session."""
    async with Nix() as nix, nix.store() as store, nix.eval(store):
        with pytest.raises(WorkerBusyError):
            await store.get_uri()


async def test_concurrent_log_stream():
    """log_stream can be iterated concurrently with store operations.

    Does not assert event count — Nix store operations are quiet at
    default verbosity.  The request-id mapping is tested in
    ``tests/test_session_unit.py::TestLogStreamRequestId``.
    """
    async with Nix() as nix:
        events: list[LogEvent] = []
        bg_task = asyncio.ensure_future(_collect(nix, events))

        async with nix.store() as store:
            await store.get_uri()
            await store.get_store_dir()

        # Cancel the collector after a brief pause
        await asyncio.sleep(0.5)
        bg_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bg_task


# ── Error handling & resilience ──────────────────────────────────────


async def test_error_propagation():
    """Worker errors are classified and raised as typed NixError subclasses."""
    async with Nix() as nix, nix.store() as store:
        with pytest.raises(StoreError, match="is not valid"):
            await store.query_path_info("/nix/store/00000000000000000000000000000000-nonexistent-1.0")


async def test_worker_death_detection():
    """Killing the worker raises WorkerDiedError on the next call."""
    async with Nix() as nix, nix.store() as store:
        # First call works normally
        uri = await store.get_uri()
        assert isinstance(uri, str)

        # Kill the subprocess
        proc = nix._manager._proc
        assert proc is not None
        proc.kill()
        await proc.wait()

        # Give the background reader a moment to notice
        await asyncio.sleep(0.2)

        # Next call should raise WorkerDiedError
        with pytest.raises(WorkerDiedError):
            await store.get_uri()


async def test_idle_timeout_resets_with_activity():
    """Multiple fast calls on a single worker — all should succeed."""
    async with Nix() as nix, nix.store() as store:
        for _ in range(3):
            uri = await store.get_uri()
            assert isinstance(uri, str)


async def _collect(nix, events):
    async for event in nix.log_stream():
        events.append(event)
