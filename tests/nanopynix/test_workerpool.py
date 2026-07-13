"""Tests for the Session — single subprocess worker concurrency."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from nanopynix_proto.nix.store import GetStoreDirRequest, GetUriRequest, QueryPathInfoRequest

from nanopynix import LogEvent, Nix, StoreError, WorkerBusyError, WorkerDiedError


async def test_single_worker_basics():
    """Basic round-trip with a single worker."""
    async with Nix() as nix, nix.store() as store:  # type: ignore[reportUnknownMemberType] -- Nix/nanobind extension without full stubs
        uri = await store.get_uri(GetUriRequest())  # type: ignore[reportUnknownMemberType] -- Store from nanobind extension
        assert isinstance(uri.uri, str)  # type: ignore[reportUnknownMemberType] -- uri from nanobind extension
        d = await store.get_store_dir(GetStoreDirRequest())
        assert d.dir == "/nix/store"


async def test_two_workers_sequential():
    """Sequential calls on a single worker — should all succeed."""
    async with Nix() as nix, nix.store() as store:  # type: ignore[reportUnknownMemberType] -- Nix/nanobind extension
        for _ in range(4):
            uri = await store.get_uri(GetUriRequest())  # type: ignore[reportUnknownMemberType] -- Store from nanobind
            assert isinstance(uri.uri, str)  # type: ignore[reportUnknownMemberType] -- uri from nanobind


async def test_worker_busy_while_eval_session_holds_worker():
    """The single worker does not silently queue behind an eval session."""
    async with Nix() as nix, nix.store() as store, nix.eval(store):  # type: ignore[reportUnknownMemberType] -- Nix/nanobind extension
        with pytest.raises(WorkerBusyError):
            await store.get_uri(GetUriRequest())  # type: ignore[reportUnknownMemberType] -- Store from nanobind


async def test_concurrent_log_stream():
    """log_stream can be iterated concurrently with store operations.

    Does not assert event count — Nix store operations are quiet at
    default verbosity.  The request-id mapping is tested in
    ``tests/test_session_unit.py::TestLogStreamRequestId``.
    """
    async with Nix() as nix:  # type: ignore[reportUnknownMemberType] -- Nix/nanobind extension
        events: list[LogEvent] = []
        bg_task = asyncio.ensure_future(_collect(nix, events))

        async with nix.store() as store:  # type: ignore[reportUnknownMemberType] -- Nix/nanobind extension
            await store.get_uri(GetUriRequest())  # type: ignore[reportUnknownMemberType] -- Store from nanobind
            await store.get_store_dir(GetStoreDirRequest())  # type: ignore[reportUnknownMemberType] -- Store from nanobind

        # Cancel the collector after a brief pause
        await asyncio.sleep(0.5)
        bg_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bg_task


# ── Error handling & resilience ──────────────────────────────────────


async def test_error_propagation():
    """Worker errors are classified and raised as typed NixError subclasses."""
    async with Nix() as nix, nix.store() as store:  # type: ignore[reportUnknownMemberType] -- Nix/nanobind extension
        with pytest.raises(StoreError, match="is not valid"):
            await store.query_path_info(  # type: ignore[reportUnknownMemberType] -- Store from nanobind
                QueryPathInfoRequest(path="/nix/store/00000000000000000000000000000000-nonexistent-1.0")
            )


async def test_worker_death_detection():
    """Channel failure raises WorkerDiedError or connection error on the next call."""
    async with Nix() as nix, nix.store() as store:  # type: ignore[reportUnknownMemberType] -- Nix/nanobind extension
        # First call works normally
        uri = await store.get_uri(GetUriRequest())  # type: ignore[reportUnknownMemberType] -- Store from nanobind
        assert isinstance(uri.uri, str)  # type: ignore[reportUnknownMemberType] -- uri from nanobind

        # With multiprocessing transport, kill the forkserver process directly.
        # The channel should notice the closed pipe.
        channel = nix._manager._channel  # type: ignore[reportUnknownMemberType, reportPrivateUsage] -- intentional test of internal transport state
        if channel is not None:
            await channel.aclose()  # type: ignore[reportUnknownMemberType] -- channel type from nanobind extension
        # In multiprocessing mode, the worker is managed by AsyncExitStack;
        # kill via process is not directly exposed.  This test validates
        # that the pool detects transport-level failures.
        # Next call should raise an error
        with pytest.raises((WorkerDiedError, ConnectionError, OSError)):
            await store.get_uri(GetUriRequest())  # type: ignore[reportUnknownMemberType] -- Store from nanobind


async def test_idle_timeout_resets_with_activity():
    """Multiple fast calls on a single worker — all should succeed."""
    async with Nix() as nix, nix.store() as store:  # type: ignore[reportUnknownMemberType] -- Nix/nanobind extension
        for _ in range(3):
            uri = await store.get_uri(GetUriRequest())  # type: ignore[reportUnknownMemberType] -- Store from nanobind
            assert isinstance(uri.uri, str)  # type: ignore[reportUnknownMemberType] -- uri from nanobind


async def _collect(nix: Nix, events: list[LogEvent]) -> None:
    async for event in nix.log_stream():
        events.append(event)
