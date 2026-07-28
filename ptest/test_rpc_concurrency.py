"""Does process isolation get past the two-evaluator ceiling inproc hits?

inproc stops scaling at two concurrent evaluators: a third and fourth burn 70%
more CPU for worse wall clock. The standing suspect is Boehm -- one collector,
one heap, one stop-the-world pause shared by every evaluator thread in the
process.

RPC puts each ``Session`` in its own worker process, so each gets its own Boehm
heap and collector. If the ceiling is Boehm, RPC should scale past it. If RPC
stalls at two as well, the suspect is wrong and something else is the limit.
That is the question this module exists to answer; the RPC overhead it pays is
the price of the answer, and is measured rather than assumed.

The grid separates the two axes deliberately:

- ``(1, 2)`` -- two evaluators in *one* worker. Same Boehm heap as inproc's
  capacity 2, plus RPC overhead. Isolates the cost of the wire.
- ``(2, 1)`` -- two evaluators in *two* workers. Two heaps. Isolates the
  benefit of isolation.
- ``(4, 1)`` vs ``(2, 2)`` -- four evaluators spread four ways or two ways.

``(1, 2)`` also settles a documentation question: ``rpc.Session``'s docstring
says it owns "one thread-confined EvalState at a time", but the worker
allocates a fresh executor and handle per ``open_eval``
(``_worker_eval.py:233``). If two evaluators per session work, the docstring is
stale.
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import TYPE_CHECKING

import anyio
import pytest
from pytest_agent import note

import nanopynix
from ptest._cpu import tree_cpu_seconds
from ptest.test_concurrent_eval import WORK_ANSWER, WORK_EXPR

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from nanopynix.rpc import EvalSession as RpcEvalSession

pytestmark = pytest.mark.concurrency

TASKS = 8

# (worker processes, evaluators per worker). Total evaluators is the product,
# so 1/2/4 line up with inproc's capacity 1/2/4.
GRID = [(1, 1), (1, 2), (2, 1), (2, 2), (4, 1)]


@pytest.fixture(scope="session")
def rpc_session_factory(
    store_root: Path,
    nix_settings: nanopynix.NixSettings,
) -> Callable[[], nanopynix.rpc.Session]:
    """RPC sessions against the *same* store root the inproc tests use.

    Several worker processes writing one local store is the configuration
    already measured safe at 1/2/4 processes, so it is not what is under test
    here -- only the evaluation is.
    """

    def make() -> nanopynix.rpc.Session:
        return nanopynix.rpc.Session(
            store_uri=f"local://?root={store_root}",
            load_config=False,
            settings=nix_settings,
        )

    return make


async def _work(evaluator: RpcEvalSession, n: int) -> int:
    result = await (await evaluator.string(f"{WORK_EXPR} + {n}")).to_python()
    if not isinstance(result, int):
        raise TypeError(f"expected int, got {type(result).__name__}")
    return result


@pytest.mark.parametrize(("workers", "evals_each"), GRID, ids=lambda v: str(v))
async def test_rpc_scales_with_workers(
    rpc_session_factory: Callable[[], nanopynix.rpc.Session],
    workers: int,
    evals_each: int,
) -> None:
    """Eight evaluations across *workers* processes, *evals_each* evaluators apiece.

    Everything expensive -- worker spawn, channel setup, EvalState creation --
    happens before the clock starts, matching how the inproc measurement
    pre-warms its pool. What is timed is evaluation and the RPC round trips it
    takes, nothing else.

    Recorded, not asserted: this is a measurement, and the correctness
    assertion is that all eight answers come back right and distinct.
    """
    results: dict[int, int] = {}

    setup_started = time.monotonic()
    async with contextlib.AsyncExitStack() as stack:
        evaluators: list[RpcEvalSession] = []
        for _ in range(workers):
            session = await stack.enter_async_context(rpc_session_factory())
            store = await stack.enter_async_context(session.store())
            for _ in range(evals_each):
                evaluators.append(await stack.enter_async_context(session.eval(store)))

        assert len(evaluators) == workers * evals_each

        # Warm every evaluator: the first evaluation in a fresh EvalState pays
        # for parsing and base-environment setup that later ones do not.
        async with anyio.create_task_group() as tg:
            for evaluator in evaluators:
                tg.start_soon(_work, evaluator, 0)

        # Worker spawn is excluded from the measurement above but is not free,
        # and a suite that spawned one per test would pay it 1668 times. This
        # is the number that says how aggressively workers must be pooled.
        setup = time.monotonic() - setup_started

        async def run(n: int) -> None:
            results[n] = await _work(evaluators[n % len(evaluators)], n)

        started = time.monotonic()
        cpu_started = tree_cpu_seconds()
        async with anyio.create_task_group() as tg:
            for n in range(TASKS):
                tg.start_soon(run, n)
        elapsed = time.monotonic() - started
        cpu = tree_cpu_seconds() - cpu_started

    assert results == {n: WORK_ANSWER + n for n in range(TASKS)}

    note(
        workers=workers,
        evals_each=evals_each,
        evaluators=workers * evals_each,
        wall=f"{elapsed:.3f}s",
        tree_cpu=f"{cpu:.3f}s",
        cpu_per_wall=f"{cpu / elapsed:.2f}",
        setup=f"{setup:.3f}s",
        setup_per_worker=f"{setup / workers:.3f}s",
        pid=os.getpid(),
    )


async def test_one_worker_hosts_several_evaluators(
    rpc_session_factory: Callable[[], nanopynix.rpc.Session],
) -> None:
    """Three concurrent evaluators in one RPC worker, giving three right answers.

    ``rpc.Session``'s docstring claims one EvalState at a time. If this passes,
    that claim is stale -- and it matters, because it is the difference between
    RPC parallelism costing one process per evaluator and costing one process
    per *group* of evaluators.
    """
    results: dict[int, int] = {}

    async with contextlib.AsyncExitStack() as stack:
        session = await stack.enter_async_context(rpc_session_factory())
        store = await stack.enter_async_context(session.store())
        evaluators = [await stack.enter_async_context(session.eval(store)) for _ in range(3)]

        async def run(n: int) -> None:
            results[n] = await _work(evaluators[n], n)

        async with anyio.create_task_group() as tg:
            for n in range(3):
                tg.start_soon(run, n)

    assert results == {n: WORK_ANSWER + n for n in range(3)}
