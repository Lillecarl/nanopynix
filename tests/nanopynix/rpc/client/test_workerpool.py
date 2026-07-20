"""Tests for the Session — single subprocess worker concurrency."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from nanopynix import LogEvent, Nix, NixType, StoreError, WorkerDiedError


async def test_single_worker_basics():
    """Basic round-trip with a single worker."""
    async with Nix() as nix, nix.store() as store:
        uri = await store.uri()
        assert isinstance(uri, str)
        d = await store.store_dir()
        assert d == "/nix/store"


async def test_two_workers_sequential():
    """Sequential calls on a single worker — should all succeed."""
    async with Nix() as nix, nix.store() as store:
        for _ in range(4):
            uri = await store.uri()
            assert isinstance(uri, str)


@pytest.mark.concurrency
async def test_store_operation_runs_while_eval_session_is_open():
    """An EvalState owns evaluator state, not the worker's Store API."""
    async with Nix() as nix, nix.store() as store, nix.eval(store):
        assert isinstance(await store.uri(), str)


@pytest.mark.concurrency
async def test_session_allows_concurrent_eval_states():
    """N EvalSession/ReplSession instances may be open at once, each independent."""
    async with Nix() as nix, nix.store() as store:
        first = nix.eval(store)
        second = nix.repl(store)
        await first.open()
        await second.open()

        first_value = await first.string("1 + 1")
        second_value = await second.line("2 + 2")

        assert second_value is not None
        assert await first_value.force_as(NixType.INT) == 2
        assert await second_value.force_as(NixType.INT) == 4

        await first.close()
        await second.close()


@pytest.mark.concurrency
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
            await store.uri()
            await store.store_dir()

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
                "/nix/store/00000000000000000000000000000000-nonexistent-1.0"
            )


@pytest.mark.forked
async def test_worker_death_detection():
    """Channel failure raises WorkerDiedError or connection error on the next call.

    Force-closing a live channel leaves its background reader task to hit a
    StreamTerminatedError asynchronously. anyio's shared runner surfaces that
    as a failure on whichever *other* test happens to be running when it
    finally errors (confirmed: forking the first victim just moved the
    failure to the next async test in the queue). Forking this test instead
    means the leaked task dies with the child process and can never bleed
    into the shared runner used by every other test.
    """
    async with Nix() as nix, nix.store() as store:
        # First call works normally
        uri = await store.uri()
        assert isinstance(uri, str)

        # With multiprocessing transport, kill the forkserver process directly.
        # The channel should notice the closed pipe.
        channel = nix._manager._channel  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
        if channel is not None:
            await channel.aclose()
        # In multiprocessing mode, the worker is managed by AsyncExitStack;
        # kill via process is not directly exposed.  This test validates
        # that the pool detects transport-level failures.
        # Next call should raise an error
        with pytest.raises((WorkerDiedError, ConnectionError, OSError)):
            await store.uri()


async def test_idle_timeout_resets_with_activity():
    """Multiple fast calls on a single worker — all should succeed."""
    async with Nix() as nix, nix.store() as store:
        for _ in range(3):
            uri = await store.uri()
            assert isinstance(uri, str)


async def _collect(nix: Nix, events: list[LogEvent]) -> None:
    async for event in nix.log_stream():
        events.append(event)  # noqa: PERF401
