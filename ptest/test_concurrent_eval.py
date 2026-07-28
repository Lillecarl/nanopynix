"""Run many evaluations at once through one pool, one store, one process.

This is the load the "async instead of xdist" idea implies, applied to our own
threading model: N evaluator threads, each with its own ``EvalState``, all
talking to one ``LocalStore``, driven from one event loop, with evaluators
being created and destroyed underneath the traffic.

Correctness assertions are hard. Timing is *recorded*, not asserted -- a wall
clock on a shared CI box is not a thing to gate on, but the numbers are the
whole reason for the exercise, so they go into the report via
``pytest_agent.note`` where a run can be compared against the last one.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import anyio
import pytest
from pytest_agent import note

from ptest._pool import pool_scope

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from nanopynix.inproc import EvalSession, Store

pytestmark = pytest.mark.concurrency

# Enough evaluation to be measurable on one thread without being slow. Sized by
# measurement, not by guess: 20k elements came out at ~4.6ms, far too cheap to
# say anything about parallelism, so it is 300k. No store access and no
# nixpkgs, so what this times is the evaluator thread and nothing else.
WORK_SIZE = 300_000
WORK_EXPR = f"builtins.foldl' (a: b: a + b) 0 (builtins.genList (x: x) {WORK_SIZE})"
WORK_ANSWER = WORK_SIZE * (WORK_SIZE - 1) // 2


async def _work(evaluator: EvalSession, n: int) -> int:
    """One unit of evaluation whose answer depends on *n*, so mix-ups show."""
    value = await evaluator.string(f"{WORK_EXPR} + {n}")
    result = await value.to_python()
    if not isinstance(result, int):
        raise TypeError(f"expected int, got {type(result).__name__}")
    return result


async def test_concurrent_evaluations_do_not_mix_up_results(
    evaluator_factory: Callable[[], EvalSession],
) -> None:
    """Sixteen tasks, four evaluators, distinct answers.

    Every task's expected value differs, so a result routed to the wrong caller
    -- the failure mode a shared executor or a mis-keyed operation id would
    produce -- fails the assertion instead of passing silently.
    """
    results: dict[int, int] = {}

    async with pool_scope(evaluator_factory, capacity=4, max_leases=100) as pool:

        async def run(n: int) -> None:
            async with pool.lease() as evaluator:
                results[n] = await _work(evaluator, n)

        async with anyio.create_task_group() as tg:
            for n in range(16):
                tg.start_soon(run, n)

        assert results == {n: WORK_ANSWER + n for n in range(16)}
        assert pool.stats.peak_live <= 4


async def test_evaluator_rotation_under_concurrent_load(
    evaluator_factory: Callable[[], EvalSession],
) -> None:
    """The lifecycle stress: evaluators die and are born while work is in flight.

    ``max_leases=2`` with 24 leases forces ~12 create/retire cycles overlapping
    live evaluation on the other slots. Opening and closing an ``EvalSession``
    registers and unregisters a thread with Boehm GC, so this is the test that
    would catch a collection racing a thread that is going away -- historically
    the shape of crash this codebase has actually had.
    """
    results: dict[int, int] = {}

    async with pool_scope(evaluator_factory, capacity=3, max_leases=2) as pool:

        async def run(n: int) -> None:
            async with pool.lease() as evaluator:
                results[n] = await _work(evaluator, n)

        async with anyio.create_task_group() as tg:
            for n in range(24):
                tg.start_soon(run, n)

        assert results == {n: WORK_ANSWER + n for n in range(24)}
        assert pool.stats.leases == 24
        assert all(count == 2 for count in pool.stats.retired_after)
        # Not exactly 12: the final round leaves up to `capacity` evaluators
        # holding one lease each, so 12 is the floor and 12 + capacity - 1 the
        # ceiling. The exact statement is the lease accounting below.
        assert 12 <= pool.stats.created <= 14

    assert pool.stats.retired == pool.stats.created
    # Every lease is accounted for by exactly one retired evaluator: nothing
    # was leased from an evaluator the pool had already forgotten about.
    assert sum(pool.stats.retired_after) == 24


@pytest.mark.parametrize("capacity", [1, 2, 4])
async def test_concurrency_scales_with_capacity(
    evaluator_factory: Callable[[], EvalSession],
    capacity: int,
) -> None:
    """How much wall clock capacity actually buys, recorded per slot count.

    Asserted only on correctness: throughput on a shared box is a measurement,
    not a contract. The recorded ratio is what says whether "parallelise with
    async" is worth building.

    **These parametrizations are not isolated from each other.** Each gets its
    own pool and its own evaluators, but they share the process: the Boehm heap
    is never reset (nothing binds a collection), the store stays warm, and the
    order is whatever pytest picks. Comparing the three numbers from one run is
    therefore suggestive, not sound -- run each in its own process to compare
    them properly::

        pytest 'ptest/test_concurrent_eval.py::test_concurrency_scales_with_capacity[4]'

    Doing that gave 0.411s / 0.225s / 0.248s for capacity 1 / 2 / 4,
    reproducible to the millisecond and stable under reordering, against
    numbers that drifted by 20% within a shared process.
    """
    tasks = 8  # two rounds at capacity=4, so queuing is exercised at every capacity
    async with pool_scope(evaluator_factory, capacity=capacity, max_leases=100) as pool:
        # Every slot, not just one: a lazily-filled pool would create
        # `capacity - 1` evaluators inside the timed window and charge the wide
        # configurations for setup the narrow ones had already finished.
        await pool.warm()
        async with pool.lease() as evaluator:
            await _work(evaluator, 0)

        results: dict[int, int] = {}

        async def run(n: int) -> None:
            async with pool.lease() as evaluator:
                results[n] = await _work(evaluator, n)

        started = time.monotonic()
        cpu_started = time.process_time()
        async with anyio.create_task_group() as tg:
            for n in range(tasks):
                tg.start_soon(run, n)
        elapsed = time.monotonic() - started
        cpu = time.process_time() - cpu_started

        assert results == {n: WORK_ANSWER + n for n in range(tasks)}

    # cpu/wall is the diagnostic that separates "not parallel" from "parallel
    # but bounded by something else": process_time sums every thread, so a
    # ratio near 1.0 at capacity 4 means the evaluator threads are serialising
    # against each other -- Boehm's stop-the-world collector being the first
    # thing to suspect, since the GIL is released in eval_string.
    note(
        capacity=capacity,
        tasks=tasks,
        wall=f"{elapsed:.3f}s",
        per_eval=f"{elapsed / tasks:.3f}s",
        cpu=f"{cpu:.3f}s",
        cpu_per_wall=f"{cpu / elapsed:.2f}",
    )


async def test_one_store_serves_concurrent_writers(
    nix_store: Store,
    tmp_path: Path,
) -> None:
    """Concurrent ``add_to_store`` against the one shared store root.

    Separately measured at 1/2/4 processes with zero errors; this is the
    in-process form of the same question, and the one that matters for the
    design, since a single process is what we are proposing to run. libstore
    retries ``SQLITE_BUSY`` itself behind a one-hour busy timeout, so a
    ``SQLiteBusy`` escaping here would be a real finding.
    """
    sources = []
    for n in range(16):
        source = tmp_path / f"source-{n}"
        await anyio.Path(source).write_text(f"contents {n}\n")
        sources.append(source)

    added: dict[int, str] = {}

    async def add(n: int) -> None:
        path = await nix_store.add_to_store(str(sources[n]), name=f"ptest-{n}")
        added[n] = str(path)

    async with anyio.create_task_group() as tg:
        for n in range(16):
            tg.start_soon(add, n)

    assert len(set(added.values())) == 16
    for path in added.values():
        assert await nix_store.is_valid_path(path)


async def test_reuse_amortises_a_nixpkgs_import(
    evaluator_factory: Callable[[], EvalSession],
) -> None:
    """The reason the pool exists, as a test rather than a scratchpad script.

    Scratchpad measurement was 0.140s fresh against 0.030s reused, and -- the
    part that matters -- a *different* attribute costs the same as a repeat,
    so the nixpkgs base is amortised in the value graph rather than memoised
    per expression. Recorded, not asserted: it depends on the host's nixpkgs
    and on whether the store is warm.
    """
    attrs = ["hello", "curl", "jq", "ripgrep"]
    async with pool_scope(evaluator_factory, capacity=1, max_leases=100) as pool, pool.lease() as evaluator:
        timings: list[float] = []
        for attr in attrs:
            started = time.monotonic()
            name = await (await evaluator.string(f"with import <nixpkgs> {{}}; {attr}.name")).to_python()
            timings.append(time.monotonic() - started)
            assert isinstance(name, str)
            assert name

        note(
            first=f"{attrs[0]}={timings[0]:.3f}s",
            subsequent=", ".join(f"{a}={t:.3f}s" for a, t in zip(attrs[1:], timings[1:], strict=True)),
        )
