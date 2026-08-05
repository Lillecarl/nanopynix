"""Tests for the Session — single subprocess worker concurrency."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import signal
from typing import TYPE_CHECKING, Any, cast

import anyio
import anyio.to_thread
import grpclib_transports.multiprocessing as mp_transport
import pytest
from grpclib.exceptions import StreamTerminatedError

from nanopynix import LogEvent, StoreClosedError, StoreError
from nanopynix.rpc import Session, WorkerDiedError, WorkerSignaledError
from nanopynix.rpc.client import _pool as pool_module, session as session_module
from nanopynix.rpc.worker._worker import worker_child_teardown
from tests.support.worker_death import expect_the_worker_to_die

if TYPE_CHECKING:
    from collections.abc import Generator


async def test_single_worker_basics():
    """Basic round-trip with a single worker."""
    async with Session() as nix, nix.store() as store:
        uri = await store.uri()
        assert isinstance(uri, str)
        d = await store.store_dir()
        assert d == "/nix/store"


async def test_two_workers_sequential():
    """Sequential calls on a single worker — should all succeed."""
    async with Session() as nix, nix.store() as store:
        for _ in range(4):
            uri = await store.uri()
            assert isinstance(uri, str)


@pytest.mark.concurrency
async def test_store_operation_runs_while_eval_session_is_open():
    """An EvalState owns evaluator state, not the worker's Store API."""
    async with Session() as nix, nix.store() as store, nix.eval(store):
        assert isinstance(await store.uri(), str)


@pytest.mark.concurrency
async def test_session_allows_concurrent_eval_states():
    """N EvalSession/ReplSession instances may be open at once, each independent."""
    async with Session() as nix, nix.store() as store:
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
    async with Session() as nix:
        events: list[LogEvent] = []
        bg_task = asyncio.ensure_future(_collect(nix, events))

        async with nix.store() as store:
            await store.uri()
            await store.store_dir()

        # Cancel the collector after a brief pause
        await anyio.sleep(0.5)
        bg_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bg_task


# ── Error handling & resilience ──────────────────────────────────────


async def test_error_propagation():
    """Worker errors are classified and raised as typed NixError subclasses."""
    async with Session() as nix, nix.store() as store:
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

    async with Session() as nix, nix.store() as store:
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
    async with Session() as nix, nix.store() as store:
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
    nix = Session()
    await nix.open()
    proc = nix._manager._worker_proc  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
    assert proc is not None
    assert proc.is_alive()

    with anyio.move_on_after(0):
        await nix.close()

    assert not proc.is_alive()


async def test_a_normal_close_lets_the_worker_end_itself():
    """A worker that closes normally exits 0. It is not signalled.

    The teardown of ``grpclib_transports`` is ``await channel.aclose()`` then
    ``await _stop_process(proc)``, and ``_stop_process`` reached ``terminate()``
    about 3 ms after the channel closed. So every healthy worker died of
    SIGTERM, and anything the worker does after ``serve_h2`` returns never ran.
    ``_stop_process`` waits for the child before it signals it now.

    The cancelled close above is the other half of the pair. The signal is
    still right there, because a cancelled shutdown asks for speed.

    ``exitcode`` carries the whole assertion: 0 says the worker ended itself,
    and -15 says the signal got there first.
    """
    nix = Session()
    await nix.open()
    proc = nix._manager._worker_proc  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
    assert proc is not None

    await nix.close()

    assert proc.exitcode == 0, f"the worker was signalled rather than left to end itself: exitcode {proc.exitcode}"


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

    The open store is load-bearing. The deadline covers handing worker-side
    resources back and nothing else, so with none to hand back there is no
    checkpoint inside it and an expired scope has no cancellation to deliver --
    correctly, since there is then nothing that could have been late.
    """
    monkeypatch.setattr(session_module, "_GRACEFUL_CLOSE_TIMEOUT_SECONDS", 0.0)
    nix = Session()
    await nix.open()
    store = nix.store()
    await store.open()
    proc = nix._manager._worker_proc  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
    assert proc is not None

    with pytest.raises(TimeoutError):
        await nix.close()

    assert not proc.is_alive()


class _FailingEval:
    """Stands in for an evaluator whose close fails.

    Registered directly in ``Session._evals`` because the point is what
    ``Session.close`` does with a failure, not how one arises: any of
    ``EvalSession.close``'s RPC, handle-release, or unregister steps can raise,
    and they all reach ``close`` the same way.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    async def close(self) -> None:
        raise RuntimeError(self._name)


async def test_one_failing_evaluator_does_not_abandon_the_rest_of_the_close():
    """A raising evaluator is reported, not allowed to strand everything after it.

    ``close`` used to await each evaluator, then each store, then the worker, in
    one unguarded run -- so the first failure skipped every remaining resource
    *and* the worker, leaving a live Nix store connection nothing in this
    process still had a reference to. Reported as a clean-looking exception, at
    that: the caller saw the evaluator's error and had no reason to suspect a
    subprocess had outlived it.

    All three consequences are asserted separately, since they are three
    separate fixes: the error reaches the caller, the worker is gone anyway,
    and the store the failure skipped past still got closed. The worker comes
    first deliberately -- it is the one that outlives the process if nobody
    checks.
    """
    nix = Session()
    await nix.open()
    store = nix.store()
    await store.open()
    proc = nix._manager._worker_proc  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
    assert proc is not None
    nix._evals.add(cast("Any", _FailingEval("evaluator close failed")))  # type: ignore[reportPrivateUsage] -- injecting a failure into the registry close() walks

    with pytest.raises(RuntimeError, match="evaluator close failed"):
        await nix.close()

    assert not proc.is_alive()
    with pytest.raises(StoreClosedError):
        await store.uri()


async def test_close_reports_every_failure_it_collected():
    """Two failures arrive as a group, not as whichever one happened first.

    ``inproc.Session.close`` has always reported them this way; matching it is
    the point. A single failure still arrives on its own -- see the test above
    -- because wrapping one exception in a group only makes it harder to catch.
    """
    nix = Session()
    await nix.open()
    proc = nix._manager._worker_proc  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
    assert proc is not None
    for name in ("first failure", "second failure"):
        nix._evals.add(cast("Any", _FailingEval(name)))  # type: ignore[reportPrivateUsage] -- injecting a failure into the registry close() walks

    with pytest.raises(BaseExceptionGroup) as excinfo:
        await nix.close()

    assert sorted(str(exc) for exc in excinfo.value.exceptions) == ["first failure", "second failure"]
    assert not proc.is_alive()


async def _collect(nix: Session, events: list[LogEvent]) -> None:
    async for event in nix.log_stream():
        events.append(event)  # noqa: PERF401 -- events is a shared list a background caller polls while this loop keeps running; a comprehension would defer visibility until the stream ends


# ── Worker death: real signals, not a closed channel ─────────────────
#
# Crash isolation is the documented reason this engine exists --
# docs/nanopynix/architecture.md promises "a worker crash/OOM raises
# WorkerDiedError -- your process survives" -- and until these tests existed
# nothing sent the worker a signal. test_worker_death_detection above
# force-closes the *channel*, and says so itself: "kill via process is not
# directly exposed". A closed channel is not a dead process. It leaves no
# half-written pipe, no child to reap, and no finalizer firing after the peer
# is gone. See issue #12.
#
# All four are `forked`, and not `concurrency` as issue #12 asks. Both parts
# were measured, not chosen. Unmarked, the run of tests/nanopynix/rpc failed
# twice: once inside test_a_killed_worker_still_lets_the_eval_session_close,
# and once in test_worker_initializes_nix_on_dedicated_thread -- an unrelated
# test several files later, with two leaked StreamTerminatedErrors. Adding
# _tolerating_the_backchannel_leak fixed the first and not the second, because
# that leak surfaces after the handler is restored. Forking contains it, which
# is the same conclusion test_worker_death_detection reached about the same
# backchannel peer. The handler stays as well: it is what keeps the first
# failure away.
#
# `concurrency` is then the marker not to add, because it would put a forked
# test into the ThreadSanitizer matrix (`-m concurrency` in
# ci/workflows/lib.nix) and pytest-forked under TSan has never run here.
# Killing a process is not a data race, so TSan has nothing to find in these
# four -- it would only add a combination nobody has tried.

# Big enough to keep the worker busy for seconds, and pure, so it needs no
# build store and no --run-temp-store-builds. The kill lands a fraction of a
# second in, which is far inside the RPC timeout -- so a TimeoutError can
# never be what a test below actually caught.
_SLOW_PURE_EVAL = "builtins.foldl' (a: b: a + b) 0 (builtins.genList (x: x) 40000000)"


def _worker_process(nix: Session) -> Any:
    """Return this session's worker, as a ``multiprocessing.Process``.

    ``WorkerClient._on_worker_process_start`` keeps the process object rather
    than only its pid, precisely so that teardown can stop it directly. That
    makes it reachable here too, which is the whole mechanism these tests
    needed -- see test_a_cancelled_close_still_stops_the_worker_process for
    the same reach.
    """
    proc = nix._manager._worker_proc  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
    assert proc is not None
    assert proc.is_alive()
    return proc


async def _reaped(proc: Any) -> None:
    """Wait for a signalled child to be collected, and assert that it was.

    A dead child that is never joined stays a zombie, so "the call raised" is
    only half of what crash isolation means.
    """
    await anyio.to_thread.run_sync(proc.join, 10.0)
    assert not proc.is_alive()
    assert proc.exitcode is not None


@contextlib.contextmanager
def _tolerating_the_backchannel_leak() -> Generator[None]:
    """Ignore the ``StreamTerminatedError`` a dead pipe leaves unretrieved.

    A dead worker breaks the read in
    ``grpclib_transports.bidi.LogicalRpcPeer._receive_loop``, which belongs to
    the backchannel control peer riding the same channel. Nothing ever
    retrieves that exception through normal control flow, so anyio's shared
    runner collects it and raises it into whichever test is running when it
    finally surfaces -- sometimes this one, sometimes an unrelated one several
    tests later. Measured both ways here before this existed.

    ``test_worker_death_detection`` above diagnosed this first, and its
    docstring is the full account. It inlines the same handler rather than
    calling this one because its window is deliberately narrower: it starts
    tolerating only *after* its first successful call, so a
    ``StreamTerminatedError`` against a live worker still fails it. The tests
    below cannot draw that line as sharply, and do not need to -- a worker
    that died before they killed it fails ``_worker_process``'s liveness
    assertion instead.
    """
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if isinstance(context.get("exception"), StreamTerminatedError):
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
    try:
        yield
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.forked
async def test_a_killed_worker_fails_the_call_that_was_in_flight():
    """SIGKILL during a call raises WorkerDiedError, which is what an OOM does.

    The evaluation is the in-flight work: it is running inside the worker when
    the signal arrives, so this covers the case the channel test cannot -- a
    request that was already accepted and can never be answered.
    """
    with anyio.fail_after(60), _tolerating_the_backchannel_leak():
        async with Session() as nix, nix.store() as store, nix.eval(store) as evaluator:
            proc = _worker_process(nix)
            pending = asyncio.ensure_future(evaluator.string(_SLOW_PURE_EVAL))
            await anyio.sleep(0.5)
            assert not pending.done(), "the evaluation finished before the kill; make it slower"

            expect_the_worker_to_die(nix)
            proc.kill()

            with pytest.raises(WorkerSignaledError) as caught:
                await pending
            # The class the caller sees says which signal, and a SIGKILL is
            # what an OOM looks like. Reading `proc.exitcode` for that is what
            # this repository did before issue #55.
            assert caught.value.signal_number == signal.SIGKILL
            await _reaped(proc)


@pytest.mark.forked
async def test_a_killed_worker_still_lets_the_eval_session_close():
    """close() must finish when the handles it would release are already gone.

    ``EvalSession.close`` releases deferred handles and then closes the
    EvalState, both over RPC. Against a dead worker both used to raise
    WorkerDiedError -- and the second raised from a ``finally``, so it replaced
    the first and the caller was told the wrong thing about a session it could
    do nothing about anyway.

    A dropped ValueProxy is load-bearing here. Its finalizer queues a release
    rather than making an RPC from GC, so the queue is non-empty when close
    runs and the release path is really exercised.
    """
    with anyio.fail_after(60), _tolerating_the_backchannel_leak():
        async with Session() as nix, nix.store() as store:
            evaluator = nix.eval(store)
            await evaluator.open()
            kept = await evaluator.string("{ a = 1; }")
            dropped = await evaluator.string("2")
            assert await kept.attr("a").as_int() == 1
            del dropped
            gc.collect()

            proc = _worker_process(nix)
            expect_the_worker_to_die(nix)
            proc.kill()
            await _reaped(proc)

            # No raise, and no hang: the deadline above is the hang assertion.
            await evaluator.close()


@pytest.mark.forked
async def test_a_fresh_session_works_after_a_worker_died():
    """A badly died child leaves the forkserver, and this process, usable.

    Nix state is process-global and the worker is started through a forkserver
    that outlives any one worker, so "the next Session still works" is not
    implied by the two tests above.
    """
    with anyio.fail_after(90), _tolerating_the_backchannel_leak():
        async with Session() as nix:
            proc = _worker_process(nix)
            expect_the_worker_to_die(nix)
            proc.kill()
            await _reaped(proc)

        async with Session() as second, second.store() as store:
            assert isinstance(await store.uri(), str)


@pytest.mark.forked
async def test_an_aborted_worker_reports_its_signal():
    """An abort inside the worker is a worker death, and says which one it was.

    ``nanopynix.init_libstore`` documents SIGABRT as the way Nix ends a process
    that asked for an experimental feature it does not have. At the client
    boundary that is indistinguishable from a kill -- both are a closed pipe --
    so the exit status is the only thing that tells the two apart, and the
    caller now gets it as the class rather than having to read the process.
    """
    with anyio.fail_after(60), _tolerating_the_backchannel_leak():
        async with Session() as nix, nix.store() as store:
            proc = _worker_process(nix)
            expect_the_worker_to_die(nix)
            os.kill(proc.pid, signal.SIGABRT)

            # Reap before calling, not after. A signal is asynchronous, so a
            # call dispatched immediately could reach a pipe that is still
            # open and then wait out the RPC timeout instead -- which would
            # pass for the wrong reason.
            await _reaped(proc)

            with pytest.raises(WorkerSignaledError) as caught:
                await store.uri()
            assert caught.value.signal_number == signal.SIGABRT
            assert caught.value.signal_name == "SIGABRT"
            assert caught.value.exit_status == -signal.SIGABRT
            # And it is still what every existing handler catches.
            assert isinstance(caught.value, WorkerDiedError)


@pytest.mark.forked
async def test_an_abort_nobody_was_waiting_for_fails_the_close():
    """A worker that dies with no call outstanding must not close quietly.

    **This is the whole subject of issue #55.** Three teardown paths suppress
    a WorkerDiedError, each of them rightly, so a crashed worker used to close
    exactly as a healthy one does. A run of the suite reported 2077 passed
    with a core dump inside its own window.

    Nothing is in flight here, and nothing asks the session anything after the
    abort. The close is the only place left that can report it.
    """
    with anyio.fail_after(60), _tolerating_the_backchannel_leak():
        nix = Session()
        await nix.open()
        proc = _worker_process(nix)
        os.kill(proc.pid, signal.SIGABRT)
        await _reaped(proc)

        with pytest.raises(WorkerSignaledError) as caught:
            await nix.close()
        assert caught.value.signal_number == signal.SIGABRT


async def test_an_orderly_close_reports_nothing():
    """The regression the test above could cause, asserted directly.

    A worker that shuts down when it is asked exits 0, which is not a signal
    and must stay silent. Without this, "close now raises for a dead worker"
    could quietly become "close now raises".
    """
    with anyio.fail_after(60):
        async with Session() as nix, nix.store() as store:
            assert isinstance(await store.uri(), str)
        assert nix._manager.unexpected_death is None  # type: ignore[reportPrivateUsage] -- the assertion is the point of the test


async def _stop_without_grace(proc: Any) -> None:
    """Stop a worker the way the transport did before it waited for one.

    The test below needs a worker that the parent signals, and a healthy worker
    ends itself before the signal now. Zeroing the grace constant is not enough:
    the thread hand-off that reads it is itself a checkpoint, and the child
    finishes during it. So this reproduces the old shape directly.
    """
    if proc.is_alive():
        proc.terminate()
        await anyio.to_thread.run_sync(proc.join, 3.0)
    if proc.is_alive():
        proc.kill()
        await anyio.to_thread.run_sync(proc.join, 3.0)


async def test_closing_twice_does_not_report_our_own_terminate(monkeypatch: pytest.MonkeyPatch):
    """The teardown's own SIGTERM is not a crash, on any number of closes.

    A worker that the transport had to terminate ends at status -15. That
    status is cached, and the second close used to read it back and report it
    as a death nobody asked for. It failed 99 LSP tests, whose fixture closes
    the session that way.

    The terminate is forced here, rather than incidental. It used to happen by
    itself, because ``_stop_process`` signalled a healthy worker about 3 ms
    after it closed the channel. A worker gets a grace period now and ends
    itself at status 0, so this test sets that grace to zero to keep the -15
    path it is about. A worker that really will not stop reaches the same
    signal in production.

    Two closes, and the second is the one that matters. ``Session.close`` is
    idempotent by contract, so this is a supported call and not an abuse.
    """
    monkeypatch.setattr(mp_transport, "_stop_process", _stop_without_grace)
    with anyio.fail_after(60):
        nix = Session()
        await nix.open()
        store = nix.store()
        await store.open()

        await nix.close()
        await nix.close()

        manager = nix._manager  # type: ignore[reportPrivateUsage] -- reading the recorded status is the assertion
        assert manager._worker_exit_status == -signal.SIGTERM, (
            "the shape this test is about changed; it no longer ends by terminate"
        )
        assert manager.unexpected_death is None


async def test_a_terminal_interrupt_does_not_kill_the_worker():
    """Ctrl-C in the REPL cancels an evaluation; it must not end the worker.

    A worker is a child of its client, so it shares the client's foreground
    process group, and a terminal sends SIGINT to that whole group.
    ``asyncio.Runner`` installs a SIGINT handler that cancels the main task and
    re-raises ``KeyboardInterrupt``, which used to end the worker. The REPL
    returned to its prompt, exactly as intended, over a grpclib traceback and
    with a dead worker behind it. ``_ignore_terminal_interrupts`` is the fix,
    and this is its guard. Removing that call turns this test red.

    Two assertions, because either alone proves little. The first says the
    worker really is in the blast radius of a terminal Ctrl-C. The second says
    it survives the blast. Together they are the whole causal chain.

    The signal goes to the worker rather than to the process group. ``killpg``
    would reach pytest too, and the pair below already covers the chain
    without that.

    Only ``WorkerClient`` may stop a worker, and a terminal is not
    ``WorkerClient``. Cancellation still reaches the worker over the wire, as
    gRPC handler cancellation -- see tests/nanopynix/test_cancel_engine_parity.py.
    """
    with anyio.fail_after(60):
        async with Session() as nix, nix.store() as store, nix.eval(store) as ev:
            proc = _worker_process(nix)

            # The precondition. A terminal signals a process group, so this is
            # what makes surviving a SIGINT the property that matters rather
            # than a coincidence of how the test delivers it.
            assert os.getpgid(proc.pid) == os.getpgid(0)

            os.kill(proc.pid, signal.SIGINT)

            # The worker is still there, and still serving. A round trip is
            # the assertion: a dead pipe would raise WorkerDiedError instead.
            assert await (await ev.string("1 + 1")).to_python() == 2
            assert proc.is_alive()
            assert isinstance(await store.uri(), str)


async def test_a_worker_tears_itself_down_when_the_client_never_asks(monkeypatch: pytest.MonkeyPatch):
    """The teardown neither hangs the child nor delays it past its grace period.

    The ``Shutdown`` is failed with ``TimeoutError``, which is one of the five
    exceptions ``WorkerConnection.close`` already swallows, so the client asks
    for no teardown at all. The evaluator is left open on purpose: on this
    path ``child_teardown`` is what closes it, and closing an evaluator means
    blocking on its own Nix thread from ``anyio``'s pool while the child is
    already shutting down. That is the deadlock this test would catch.

    **What this does not prove.** ``exitcode`` was 0 on this path before the
    hook existed as well -- Python's own atexit join ends the evaluator
    threads cleanly, it just never runs ``_exit_evaluator_thread`` on them.
    So 0 here is a regression guard on the added work, and it is not evidence
    that the teardown ran.

    Three tests carry that chain instead, each proving one link:
    ``test_multiprocessing_worker_runs_its_child_teardown`` (the transport
    calls the hook), ``test_shutdown_worker_closes_an_evaluator_the_client_
    left_open`` (the teardown closes evaluators), and the wiring test below
    (nanopynix hands that teardown to that hook).
    """

    async def _shutdown_times_out(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError

    with anyio.fail_after(60):
        nix = Session()
        await nix.open()
        store = nix.store()
        await store.open()
        evaluator = nix.eval(store)
        await evaluator.open()
        assert await (await evaluator.string("1 + 1")).to_python() == 2

        proc = nix._manager._worker_proc  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
        assert proc is not None
        stub = nix._manager._worker_service_stub  # type: ignore[reportPrivateUsage] -- forcing the one branch this test is about
        assert stub is not None
        monkeypatch.setattr(stub, "shutdown", _shutdown_times_out)

        await nix.close()

    assert proc.exitcode == 0, f"the worker did not finish its own teardown: exitcode {proc.exitcode}"


async def test_the_pool_hands_the_worker_teardown_to_the_transport(monkeypatch: pytest.MonkeyPatch):
    """``_open_worker`` passes ``worker_child_teardown`` as ``child_teardown``.

    The link the other two tests cannot see. The transport proves that it
    calls whatever hook it is given, and the worker unit test proves what that
    teardown does; neither notices if nanopynix stops passing it. A child that
    silently gets no hook again is exactly the state this issue found.

    The spawn is cut short with a sentinel, because the wiring is decided
    before the worker starts and nothing after it is this test's subject.
    """

    class _SpawnedError(Exception):
        """Ends `open()` at the call being inspected."""

    seen: dict[str, Any] = {}

    def _capture(*_args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise _SpawnedError

    monkeypatch.setattr(pool_module, "multiprocessing_worker_with_backchannel", _capture)

    nix = Session()
    with pytest.raises(_SpawnedError):
        await nix.open()

    # `cast` is identity at run time, so this is the function itself.
    assert seen["child_teardown"] is worker_child_teardown
