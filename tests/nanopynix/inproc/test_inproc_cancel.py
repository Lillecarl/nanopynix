"""A cancelled operation must stop the Nix work, not just free the caller.

Issue #37. Before this, ``concurrent.futures.Future.cancel()`` did nothing once
the work had started, so a timeout freed the caller and abandoned the thread.
An ``EvalState`` belongs to one thread, so the next call queued behind work
nobody wanted. Measured on the tree before the fix:

    caller unblocked after 3.00s (fail_after fired)
    has_pending_work() right after : True
    has_pending_work() 4s later    : True
    follow-up eval OK after 3.62s          <-- queued behind the abandoned work

Nix answers an interrupt only where it polls ``checkInterrupt()``. libstore has
33 such calls and libexpr has 4, all in value printing: ``eval.cc`` has none. So
these tests come in two halves, and both halves are the real behaviour rather
than one being a workaround for the other.

**Every test here waits for its own abandoned thread to finish.** An abandoned
thread keeps a Nix evaluation running, and that evaluation keeps allocating from
the Boehm collector. Boehm stops the world by signalling each registered thread
and gives all of them about 0.45s in total to answer (150 retries, 3ms apart).
A test that returned while its thread was still folding would leave that budget
to be met by whatever ran next, and the abort -- "Signals delivery fails
constantly" -- would land on an unrelated test.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import anyio
import pytest
from nanopynix_bindings import errors as nanopynix_errors

from nanopynix._core import _nix_executor
from nanopynix.exceptions import EvaluatorAbandonedError

if TYPE_CHECKING:
    from nanopynix.inproc import EvalSession
    from tests.support.nix_environment import InprocSessionFactory

# Value printing calls checkInterrupt() per node (value-to-json.cc:17), so this
# is interruptible. Big enough that the deadline lands in the middle of it.
INTERRUPTIBLE = "builtins.genList (x: x) 12000000"

# A fold has no checkInterrupt() anywhere in its path. This is the case Nix
# cannot stop, and the one that abandons the evaluator.
#
# Sized to run for a few seconds, not for minutes. The size decides how much
# Boehm heap the abandoned thread holds while it runs, and a 4-core machine
# with three of these at once swaps hard enough to break the collector's
# stop-the-world budget. Only the ratio to GRACE matters to what is under test.
UNINTERRUPTIBLE = "builtins.foldl' (a: b: a + b) 0 (builtins.genList (x: x) 12000000)"

# Short enough that poisoning happens well inside the fold above, so the test
# does not depend on how fast the machine is.
GRACE = 0.3

# How long a cancelled fold may take to run itself out. Generous: the point is
# to fail with a clear message rather than to measure the machine.
ABANDONED_WORK_TIMEOUT = 60.0


@pytest.fixture
def short_interrupt_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give Nix a short grace, so an abandoned fold is over sooner.

    For the tests that are *about* the abandoned case, where the grace is dead
    time by definition. Not for the interruptible test: Nix takes about 0.4s to
    reach its next checkInterrupt() on a list this size, so a short grace there
    would poison the very path that test proves recovers.

    The executor reads this constant at construction, so it must be patched
    before the evaluator is built.
    """
    monkeypatch.setattr(_nix_executor, "DEFAULT_INTERRUPT_GRACE_SECONDS", GRACE)


async def wait_for_the_abandoned_work(evaluator: EvalSession) -> None:
    """Block until the abandoned thread has run its operation out.

    Polls, because an abandoned operation is deliberately not awaitable: the
    executor is poisoned, and ``drain()`` returns at once on a poisoned
    executor so that a caller can never be made to wait for work that may never
    end. A test is the one place that does want to wait.
    """
    with anyio.fail_after(ABANDONED_WORK_TIMEOUT):
        while evaluator.has_pending_work():  # noqa: ASYNC110 -- see above: there is no event to await
            await anyio.sleep(0.05)


@pytest.mark.anyio
async def test_a_cancelled_interruptible_operation_frees_the_evaluator(
    inproc_session: InprocSessionFactory,
) -> None:
    """The thread stops, and the evaluator is immediately usable again."""
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as evaluator:
        value = await evaluator.string(INTERRUPTIBLE)
        with anyio.move_on_after(1.0):
            await value.to_python()

        # The assertion that fails without the interrupt: the work is really
        # over, rather than still running on a thread nobody is watching.
        assert not evaluator.has_pending_work()
        assert evaluator._executor.poisoned is None  # type: ignore[reportPrivateUsage] -- the state under test

        # And the evaluator answers at once instead of queueing behind it.
        started = time.monotonic()
        result = await evaluator.string("1 + 1")
        assert await result.to_python() == 2
        assert time.monotonic() - started < 1.0


@pytest.mark.anyio
@pytest.mark.usefixtures("short_interrupt_grace")
async def test_a_cancelled_evaluation_abandons_the_evaluator(
    inproc_session: InprocSessionFactory,
) -> None:
    """A pure evaluation cannot be stopped, so the evaluator is abandoned.

    Not a lesser outcome than the test above -- it is the whole point. The old
    behaviour queued every later call behind the runaway work and hung in
    ``close()``. Failing at once, with a class that says why, is the correction.
    """
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as evaluator:
        with anyio.move_on_after(0.4):
            await evaluator.string(UNINTERRUPTIBLE)

        assert evaluator._executor.poisoned is not None  # type: ignore[reportPrivateUsage] -- the state under test

        started = time.monotonic()
        with pytest.raises(EvaluatorAbandonedError):
            await evaluator.string("1 + 1")
        # Fails immediately. The old behaviour blocked for the whole fold.
        assert time.monotonic() - started < 1.0

        await wait_for_the_abandoned_work(evaluator)


@pytest.mark.anyio
@pytest.mark.usefixtures("short_interrupt_grace")
async def test_closing_an_abandoned_evaluator_does_not_hang_or_raise(
    inproc_session: InprocSessionFactory,
) -> None:
    """Leaving the ``async with`` must work even when Nix refused to stop.

    ``close()`` used to submit the thread finalizer to the one busy worker and
    block for ever. A caller cannot be made to hang by work it already
    cancelled, so closing queues the finalizer instead of waiting for it.

    That queueing is what the last assertion is for. Skipping the finalizer
    would let the thread reach the pool's stop marker and exit *still registered
    with Boehm GC*; a later collection then calls pthread_kill on a dead tid and
    aborts the whole process. The thread must outlive its own registration, so
    the only safe order is finalizer first, exit second.
    """
    async with inproc_session() as nix, nix.store() as store:
        evaluator = nix.eval(store)
        await evaluator.open()
        with anyio.move_on_after(0.4):
            await evaluator.string(UNINTERRUPTIBLE)
        assert evaluator._executor.poisoned is not None  # type: ignore[reportPrivateUsage] -- the state under test

        with anyio.fail_after(10):
            await evaluator.close()

        await wait_for_the_abandoned_work(evaluator)

        # `_pool._threads` is the only handle on the abandoned OS thread.
        threads = tuple(evaluator._executor._pool._threads)  # type: ignore[reportPrivateUsage] -- no public view of the pool's threads
        with anyio.fail_after(10):
            # Same reason as wait_for_the_abandoned_work: nothing signals this.
            while any(thread.is_alive() for thread in threads):  # noqa: ASYNC110 -- a Thread offers no async join
                await anyio.sleep(0.05)


@pytest.mark.anyio
@pytest.mark.usefixtures("short_interrupt_grace")
async def test_an_abandoned_evaluator_does_not_stop_its_siblings(
    inproc_session: InprocSessionFactory,
) -> None:
    """Poisoning is per-evaluator, because the interrupt hook is per-thread.

    ``nix::unix::interruptCheck`` is ``thread_local``, and each evaluator owns
    one thread. The process-global ``_isInterrupted`` is deliberately never
    touched, and this is what that buys.
    """
    async with (
        inproc_session() as nix,
        nix.store() as store,
        nix.eval(store) as doomed,
        nix.eval(store) as healthy,
    ):
        with anyio.move_on_after(0.4):
            await doomed.string(UNINTERRUPTIBLE)
        assert doomed._executor.poisoned is not None  # type: ignore[reportPrivateUsage] -- the state under test

        result = await healthy.string("40 + 2")
        assert await result.to_python() == 42

        await wait_for_the_abandoned_work(doomed)


@pytest.mark.anyio
async def test_a_cancelled_operation_raises_operation_cancelled(
    inproc_session: InprocSessionFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A timeout must not arrive as ``KeyboardInterrupt``.

    ``nix_errors.cpp`` maps ``nix::Interrupted`` to ``KeyboardInterrupt``, and
    the REPL reads that as the user asking to stop. A deadline is not that, so
    the translator reads the armed token and raises ``OperationCancelled``
    instead.

    The debug log is where the exception is visible. No caller is waiting for
    it -- the caller gets its own cancellation -- so the executor retrieves it,
    logs it, and drops it.
    """
    caplog.set_level(logging.DEBUG, logger="nanopynix._core._nix_executor")
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as evaluator:
        value = await evaluator.string(INTERRUPTIBLE)
        with anyio.move_on_after(1.0):
            await value.to_python()
        assert evaluator._executor.poisoned is None  # type: ignore[reportPrivateUsage] -- the interrupt has to have worked for this test to mean anything

    records = [record for record in caplog.records if record.msg == "cancelled Nix work finished with an exception"]
    assert records, "the cancelled work did not raise, so nothing reached Nix"
    exc_info = records[-1].exc_info
    assert exc_info is not None
    raised = exc_info[1]
    assert isinstance(raised, nanopynix_errors.OperationCancelled)
    assert not isinstance(raised, KeyboardInterrupt)


@pytest.mark.anyio
async def test_an_operation_that_runs_a_nix_thread_pool_still_completes(
    inproc_session: InprocSessionFactory,
) -> None:
    """Arming the hook must not disturb Nix's own use of it.

    ``Store::queryMissing`` runs a ``ThreadPool``, and ``ThreadPool::doWork``
    assigns ``nix::unix::interruptCheck`` on each worker it spawns. The scope
    composes rather than replaces so that the two can coexist, and this is the
    operation that puts them together.
    """
    async with inproc_session() as nix, nix.store() as store:
        store_dir = (await store.store_dir()).rstrip("/")
        # A well-formed derivation that no store contains. "unknown" is a
        # perfectly good answer; the point is that the pool runs and returns.
        absent = f"{store_dir}/{'a' * 32}-nanopynix-cancel-probe.drv"

        result = await store.query_missing([absent])
        assert [str(path) for path in result.unknown] == [absent]

        # And the hook still belongs to this evaluator afterwards.
        async with nix.eval(store) as evaluator:
            value = await evaluator.string(INTERRUPTIBLE)
            with anyio.move_on_after(1.0):
                await value.to_python()
            assert not evaluator.has_pending_work()
            assert evaluator._executor.poisoned is None  # type: ignore[reportPrivateUsage] -- the state under test
