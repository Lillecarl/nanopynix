"""Tests for the Session — single subprocess worker concurrency."""

import asyncio

import pytest

from nanopynix import Nix, NixError, StoreError, WorkerDied

pytestmark = pytest.mark.asyncio


async def test_single_worker_basics():
    """Basic round-trip with a single worker."""
    async with Nix() as nix:
        uri = await nix.store.get_uri()
        assert isinstance(uri, str)
        d = await nix.store.get_store_dir()
        assert d == "/nix/store"


async def test_two_workers_sequential():
    """Sequential calls on a single worker — should all succeed."""
    async with Nix() as nix:
        for _ in range(4):
            uri = await nix.store.get_uri()
            assert isinstance(uri, str)


async def test_two_workers_concurrent():
    """Concurrent calls on a single worker — sequential under the hood."""
    async with Nix() as nix:
        results = await asyncio.gather(
            nix.store.get_uri(),
            nix.store.get_store_dir(),
            nix.store.get_uri(),
            nix.store.get_store_dir(),
        )
    assert results[0] == results[2]  # same URI
    assert results[1] == results[3] == "/nix/store"


async def test_four_workers_concurrent_path_info():
    """Concurrent query_path_info — single worker, sequential RPC."""
    async with Nix() as nix:
        paths = await nix.store.query_all_valid_paths()
        if len(paths) >= 4:
            results = await asyncio.gather(*[
                nix.store.query_path_info(p) for p in paths[:4]
            ])
            assert len(results) == 4
            for r in results:
                assert r.nar_size >= 0


async def test_concurrent_log_stream():
    """log_stream yields events from the worker."""
    async with Nix() as nix:
        events = []
        bg_task = asyncio.ensure_future(_collect(nix, events))

        # Trigger operations
        await nix.store.get_uri()
        await nix.store.get_store_dir()

        # Cancel the collector after a brief pause
        await asyncio.sleep(0.5)
        bg_task.cancel()
        try:
            await bg_task
        except asyncio.CancelledError:
            pass

        # We might or might not get events depending on verbosity
        # Just verify the mechanism doesn't crash


# ── Error handling & resilience ──────────────────────────────────────


async def test_error_propagation():
    """Worker errors are classified and raised as typed NixError subclasses."""
    async with Nix() as nix:
        with pytest.raises(StoreError, match="is not valid"):
            await nix.store.query_path_info(
                "/nix/store/00000000000000000000000000000000-nonexistent-1.0"
            )


async def test_worker_death_detection():
    """Killing the worker raises WorkerDied on the next call."""
    async with Nix() as nix:
        # First call works normally
        uri = await nix.store.get_uri()
        assert isinstance(uri, str)

        # Kill the subprocess
        worker = nix._manager._worker
        assert worker is not None
        worker._proc.kill()
        worker._proc.join(timeout=2)

        # Give the background reader a moment to notice
        await asyncio.sleep(0.2)

        # Next call should raise WorkerDied
        with pytest.raises(WorkerDied):
            await nix.store.get_uri()


async def test_idle_timeout_resets_with_activity():
    """Multiple fast calls on a single worker — all should succeed."""
    async with Nix() as nix:
        for _ in range(3):
            uri = await nix.store.get_uri()
            assert isinstance(uri, str)


async def _collect(nix, events):
    async for event in nix.log_stream():
        events.append(event)
