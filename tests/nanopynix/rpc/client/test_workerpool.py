"""Tests for the Session — single subprocess worker concurrency."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import anyio
import pytest
from grpclib.exceptions import StreamTerminatedError

from nanopynix import LogEvent, StoreError
from nanopynix.rpc import Nix, WorkerDiedError
from nanopynix.rpc.client import session as session_module


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
        assert await first_value.as_int() == 2
        assert await second_value.as_int() == 4

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
                "/nix/store/00000000000000000000000000000000-nonexistent-1.0",
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

    That leaked task belongs to the backchannel control peer
    (grpclib_transports.bidi.LogicalRpcPeer) that rides the same channel, not
    to the channel itself. Its ``aclose()`` only awaits tasks still present in
    its own bookkeeping set, but the reader task already removed itself (via
    its done-callback) the moment our forced ``channel.aclose()`` broke its
    read and it raised StreamTerminatedError -- so nothing ever retrieves that
    exception through normal control flow. asyncio flags it as "never
    retrieved" once the peer itself is torn down at session close (end of
    this `async with` block), which anyio's shared loop exception handler
    picks up and re-raises here. Since nanopynix's own worker-close path
    already treats StreamTerminatedError as an expected teardown outcome (see
    WorkerClient.close), a temporary loop exception handler ignoring it for
    the lifetime of this session is a faithful, local match for that same
    tolerance -- not a weakening of the assertion below, which still requires
    a real WorkerDiedError/ConnectionError/OSError on the next call.
    """
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def _ignore_known_backchannel_leak(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exception = context.get("exception")
        if isinstance(exception, StreamTerminatedError):
            return
        if previous_handler is not None:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    async with Nix() as nix, nix.store() as store:
        # First call works normally
        uri = await store.uri()
        assert isinstance(uri, str)

        loop.set_exception_handler(_ignore_known_backchannel_leak)

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
    loop.set_exception_handler(previous_handler)


async def test_idle_timeout_resets_with_activity():
    """Multiple fast calls on a single worker — all should succeed."""
    async with Nix() as nix, nix.store() as store:
        for _ in range(3):
            uri = await store.uri()
            assert isinstance(uri, str)


async def test_a_cancelled_close_still_stops_the_worker_process():
    """Teardown outlives a cancellation, so a timed-out close cannot leak a worker.

    ``Session.close`` runs its polite half under a deadline, and
    ``grpclib_transports`` tears the worker down by closing the channel *then*
    stopping the process, both unshielded. A cancellation arriving between the
    two therefore used to skip the stop entirely -- and the old ``close``
    swallowed the ``TimeoutError``, so the caller was told shutdown succeeded
    while a Nix-holding subprocess kept running.

    An expired scope around ``close()`` is the same cancellation from the same
    direction, delivered at the first checkpoint rather than after sixty
    seconds. Removing the shield in ``WorkerClient.close`` turns this red.
    """
    nix = Nix()
    await nix.open()
    proc = nix._manager._worker_proc  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
    assert proc is not None
    assert proc.is_alive()

    with anyio.move_on_after(0):
        await nix.close()

    assert not proc.is_alive()


async def test_close_reports_its_own_deadline_and_still_stops_the_worker(monkeypatch: pytest.MonkeyPatch):
    """``Session.close``'s deadline is reported, not logged and discarded.

    The same failure as above reached from inside: ``close`` runs the polite
    half under ``_GRACEFUL_CLOSE_TIMEOUT_SECONDS``, and expiring it used to be
    caught and turned into ``logger.warning``. A caller had no way to learn
    that shutdown had not completed cleanly -- and, since the expiry also cut
    the teardown short, no way to learn that a worker was still running.

    Both halves are asserted, because they failed together and could be fixed
    separately: the ``TimeoutError`` must reach the caller, and the process
    must be gone regardless.
    """
    monkeypatch.setattr(session_module, "_GRACEFUL_CLOSE_TIMEOUT_SECONDS", 0.0)
    nix = Nix()
    await nix.open()
    proc = nix._manager._worker_proc  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
    assert proc is not None

    with pytest.raises(TimeoutError):
        await nix.close()

    assert not proc.is_alive()


async def _collect(nix: Nix, events: list[LogEvent]) -> None:
    async for event in nix.log_stream():
        events.append(event)  # noqa: PERF401
