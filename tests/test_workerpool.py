"""Tests for the WorkerPool — multi-worker subprocess concurrency."""

import asyncio

import pytest

from nanopynix import Nix

pytestmark = pytest.mark.asyncio


async def test_single_worker_basics():
    """Basic round-trip with a single worker."""
    async with Nix(max_workers=1) as nix:
        uri = await nix.store.get_uri()
        assert isinstance(uri, str)
        d = await nix.store.get_store_dir()
        assert d == "/nix/store"


async def test_two_workers_sequential():
    """Two workers, sequential calls — should route to both."""
    async with Nix(max_workers=2) as nix:
        for _ in range(4):
            uri = await nix.store.get_uri()
            assert isinstance(uri, str)


async def test_two_workers_concurrent():
    """Two workers handling concurrent requests."""
    async with Nix(max_workers=2) as nix:
        results = await asyncio.gather(
            nix.store.get_uri(),
            nix.store.get_store_dir(),
            nix.store.get_uri(),
            nix.store.get_store_dir(),
        )
    assert results[0] == results[2]  # same URI
    assert results[1] == results[3] == "/nix/store"


async def test_four_workers_concurrent_path_info():
    """Concurrent query_path_info across multiple workers."""
    async with Nix(max_workers=4) as nix:
        paths = await nix.store.query_all_valid_paths()
        if len(paths) >= 4:
            results = await asyncio.gather(*[
                nix.store.query_path_info(p) for p in paths[:4]
            ])
            assert len(results) == 4
            for r in results:
                assert r.nar_size >= 0


async def test_concurrent_log_stream():
    """log_stream yields events from concurrent workers."""
    async with Nix(max_workers=2) as nix:
        # Start collecting log events
        events = []
        bg_task = asyncio.ensure_future(_collect(nix, events))

        # Trigger concurrent operations
        await asyncio.gather(
            nix.store.get_uri(),
            nix.store.get_store_dir(),
        )

        # Cancel the collector after a brief pause
        await asyncio.sleep(0.5)
        bg_task.cancel()
        try:
            await bg_task
        except asyncio.CancelledError:
            pass

        # We might or might not get events depending on verbosity
        # Just verify the mechanism doesn't crash


async def _collect(nix, events):
    async for event in nix.log_stream():
        events.append(event)
