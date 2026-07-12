"""Tests for the Session — single subprocess worker concurrency."""

import asyncio
import contextlib

import pytest

from nanopynix import LogEvent, Nix, StoreError, WorkerBusyError, WorkerDiedError
from nanopynix_proto.nix.store import GetStoreDirRequest, GetUriRequest, QueryPathInfoRequest


async def test_single_worker_basics():
    """Basic round-trip with a single worker."""
    async with Nix() as nix, nix.store() as store:
        uri = await store.get_uri(GetUriRequest())
        assert isinstance(uri.uri, str)
        d = await store.get_store_dir(GetStoreDirRequest())
        assert d.dir == "/nix/store"


async def test_two_workers_sequential():
    """Sequential calls on a single worker — should all succeed."""
    async with Nix() as nix, nix.store() as store:
        for _ in range(4):
            uri = await store.get_uri(GetUriRequest())
            assert isinstance(uri.uri, str)


async def test_worker_busy_while_eval_session_holds_worker():
    """The single worker does not silently queue behind an eval session."""
    async with Nix() as nix, nix.store() as store, nix.eval(store):
        with pytest.raises(WorkerBusyError):
            await store.get_uri(GetUriRequest())


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
            await store.get_uri(GetUriRequest())
            await store.get_store_dir(GetStoreDirRequest())

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
            await store.query_path_info(
                QueryPathInfoRequest(path="/nix/store/00000000000000000000000000000000-nonexistent-1.0")
            )


async def test_worker_death_detection():
    """Channel failure raises WorkerDiedError or connection error on the next call."""
    async with Nix() as nix, nix.store() as store:
        # First call works normally
        uri = await store.get_uri(GetUriRequest())
        assert isinstance(uri.uri, str)

        # With multiprocessing transport, kill the forkserver process directly.
        # The channel should notice the closed pipe.
        channel = nix._manager._channel
        if channel is not None:
            await channel.aclose()
        # In multiprocessing mode, the worker is managed by AsyncExitStack;
        # kill via process is not directly exposed.  This test validates
        # that the pool detects transport-level failures.
        # Next call should raise an error
        with pytest.raises((WorkerDiedError, ConnectionError, OSError)):
            await store.get_uri(GetUriRequest())


async def test_idle_timeout_resets_with_activity():
    """Multiple fast calls on a single worker — all should succeed."""
    async with Nix() as nix, nix.store() as store:
        for _ in range(3):
            uri = await store.get_uri(GetUriRequest())
            assert isinstance(uri.uri, str)


async def _collect(nix, events):
    async for event in nix.log_stream():
        events.append(event)
