"""Cancelling an operation must mean the same thing on both engines.

The parity rule in ``docs/nanopynix/architecture-principles.md`` says process
isolation is the only thing rpc has that inproc does not. rpc *could* kill its
worker and start again; it deliberately does not, because then a caller would
have to know which engine it held before it could tell what a timeout costs.

So both engines answer a cancellation the same way: interrupt, a bounded grace,
then ``EvaluatorAbandonedError`` on every later call. Only what happens to the
abandoned thread differs, and that is invisible from here -- which is the
point of the test.

Behaviour only, no internals. ``EvalSession._executor`` exists on inproc and
not on rpc, so the tests in ``inproc/test_inproc_cancel.py`` assert on the
poisoned flag and these assert on what a caller can see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import pytest

from nanopynix.exceptions import EvaluatorAbandonedError

if TYPE_CHECKING:
    from tests.support.nix_environment import InprocSessionFactory, RpcSessionFactory

# Value printing polls checkInterrupt(), so Nix answers this one.
INTERRUPTIBLE = "builtins.genList (x: x) 12000000"

# When to cancel the work above. It must land after the work starts and well
# before the work ends. See the same constant in inproc/test_inproc_cancel.py,
# which records the CI failure that chose the value.
CANCEL_AFTER = 0.2

# A fold polls nothing. Sized to outlast the default 2s grace plus the deadline
# below on both engines, because the worker's grace cannot be reached from this
# process and both engines must therefore reach the same state by the clock.
UNINTERRUPTIBLE = "builtins.foldl' (a: b: a + b) 0 (builtins.genList (x: x) 40000000)"

DEADLINE = 0.5

# The abandoned fold runs itself out in a handful of seconds. Waiting for it is
# what keeps the inproc case from leaving a thread inside Nix for a later test
# to trip over; see the module docstring of inproc/test_inproc_cancel.py.
ABANDONED_WORK_TIMEOUT = 120.0

# How long the refusal may take to appear. Covers the worker's own 2s grace
# plus the round trip, with room for a loaded machine.
SETTLE_TIMEOUT = 60.0


@pytest.fixture(params=["inproc", "rpc"])
def session_factory(
    request: pytest.FixtureRequest,
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> Any:
    return inproc_session if request.param == "inproc" else rpc_session


async def _wait_for_the_abandoned_work(evaluator: Any) -> None:
    """Let an abandoned operation finish, where the engine can report it.

    rpc cannot: the work is in another process, and the client holds no handle
    on it. That process is disposable and ends on its own, which is the whole
    reason the asymmetry is allowed.
    """
    has_pending_work = getattr(evaluator, "has_pending_work", None)
    if has_pending_work is None:
        return
    with anyio.fail_after(ABANDONED_WORK_TIMEOUT):
        while has_pending_work():  # noqa: ASYNC110 -- an abandoned operation is deliberately not awaitable
            await anyio.sleep(0.05)


@pytest.mark.anyio
async def test_a_cancelled_interruptible_operation_frees_the_evaluator(
    session_factory: Any,
) -> None:
    """Nix stops, and the very next call is answered at once."""
    async with session_factory() as nix, nix.store() as store, nix.eval(store) as evaluator:
        value = await evaluator.string(INTERRUPTIBLE)
        with anyio.move_on_after(CANCEL_AFTER) as scope:
            await value.to_python()
        assert scope.cancelled_caught, "the work ended before the deadline, so this test cancelled nothing"

        # Without the interrupt this queues behind the whole conversion.
        with anyio.fail_after(5.0):
            result = await evaluator.string("1 + 1")
            assert await result.to_python() == 2


# **A build with no collector cannot run this test, and the reason is the
# subject of the test itself.** It abandons an evaluator in the middle of
# `UNINTERRUPTIBLE`, which is a fold over 40 million elements that Nix will
# not stop. On rpc the worker process ends and the operating system takes the
# memory back. On inproc the abandoned work keeps allocating in the pytest
# process, and without a collector nothing ever reclaims it: a measured run
# against nix_2_34-nogc grew past 6 GB here and the kernel killed it. Forking
# the test does not help, because the child is what grows.
#
# The marker skips both engines, and the rpc half would in fact pass. Marking
# one parametrisation is not available here: `session_factory` requests both
# engine fixtures, so nothing in the fixture closure tells the two apart.
@pytest.mark.nix_capability("boehm_gc")
@pytest.mark.anyio
async def test_a_cancelled_evaluation_abandons_the_evaluator(
    session_factory: Any,
) -> None:
    """The same exception, on both engines, for work Nix will not stop."""
    async with session_factory() as nix, nix.store() as store:
        evaluator = nix.eval(store)
        await evaluator.open()
        try:
            with anyio.move_on_after(DEADLINE):
                await evaluator.string(UNINTERRUPTIBLE)

            # Both engines settle on the same refusal, and it is permanent.
            #
            # They do not reach it at the same moment, and that difference is
            # forced by process isolation. On inproc the grace runs inside the
            # cancelled caller, so the state is settled the instant the cancel
            # returns. On rpc the cancel only closes the gRPC stream; the
            # worker then runs its own grace in its own process, so a client
            # call made inside that window is served rather than refused.
            # Same class, same permanence, later moment.
            with anyio.fail_after(SETTLE_TIMEOUT):
                while True:
                    try:
                        await evaluator.string("1 + 1")
                    except EvaluatorAbandonedError:
                        break
                    await anyio.sleep(0.1)

            # And closing does not hang on the operation that would not stop.
            with anyio.fail_after(30):
                await evaluator.close()
        finally:
            await _wait_for_the_abandoned_work(evaluator)
