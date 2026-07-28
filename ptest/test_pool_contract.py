"""What the evaluator pool promises, asserted on the pool's own bookkeeping.

These are contract tests, not benchmarks: every assertion is against
``PoolStats``, never against a duration. Timing belongs in
test_concurrent_eval.py, where it is recorded rather than asserted.

The one measurement that *is* here is the premise the whole design rests on --
that a reused evaluator gives the same answers a fresh one would. If that is
not true, none of the rest is worth having.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest

from nanopynix.inproc import EvalSession
from ptest._pool import EvalStatePool, pool_scope

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize(("capacity", "max_leases"), [(0, 1), (-1, 1), (1, 0), (1, -3)])
def test_rejects_nonsensical_bounds(
    evaluator_factory: Callable[[], EvalSession],
    capacity: int,
    max_leases: int,
) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        EvalStatePool(evaluator_factory, capacity=capacity, max_leases=max_leases)


async def test_rotates_after_max_leases(
    evaluator_factory: Callable[[], EvalSession],
) -> None:
    """Seven sequential leases of a one-slot pool with a budget of three.

    Expect three evaluators: two retired at exactly three leases, the third
    still holding one. Asserting on ``retired_after`` rather than just the
    count is what catches an off-by-one that rotates a lease early or late.
    """
    async with pool_scope(evaluator_factory, capacity=1, max_leases=3) as pool:
        for _ in range(7):
            async with pool.lease() as evaluator:
                assert await (await evaluator.string("1 + 1")).to_python() == 2

        assert pool.stats.leases == 7
        assert pool.stats.created == 3
        assert pool.stats.retired_after == [3, 3]
        assert pool.stats.peak_live == 1


async def test_reused_evaluator_answers_like_a_fresh_one(
    evaluator_factory: Callable[[], EvalSession],
) -> None:
    """The premise: reuse must not change results.

    Distinct expressions, deliberately including ones that touch evaluator
    state a naive cache would get wrong -- ``builtins.currentSystem`` (read
    from settings), a thunk forced twice, and a failing expression sandwiched
    between two good ones.
    """
    expected: list[tuple[str, object]] = [
        ("1 + 1", 2),
        ('"a" + "b"', "ab"),
        ("builtins.length [ 1 2 3 ]", 3),
        ("let x = 6 * 7; in [ x x ]", [42, 42]),
        ("builtins.isString builtins.currentSystem", True),
        ("{ a = 1; b = { c = 2; }; }", {"a": 1, "b": {"c": 2}}),
    ]
    async with pool_scope(evaluator_factory, capacity=1, max_leases=1000) as pool:
        async with pool.lease() as evaluator:
            for expr, want in expected:
                assert await (await evaluator.string(expr)).to_python() == want

            # A failing evaluation must not poison the evaluator for the next
            # expression -- inside one lease we keep using it regardless.
            with pytest.raises(Exception, match="undefined variable"):
                await (await evaluator.string("nope")).to_python()
            assert await (await evaluator.string("1 + 1")).to_python() == 2

        assert pool.stats.created == 1


async def test_a_failed_lease_retires_its_evaluator(
    evaluator_factory: Callable[[], EvalSession],
) -> None:
    """An exception escaping the block must not return the evaluator to idle.

    We cannot distinguish "the assertion failed" from "the evaluator is now in
    a state that will fail the next test too", so the pool assumes the worse
    of the two. This costs one evaluator per failing test and buys the
    guarantee that a failure does not cascade.
    """
    async with pool_scope(evaluator_factory, capacity=1, max_leases=100) as pool:
        with pytest.raises(RuntimeError, match="deliberate"):
            async with pool.lease() as evaluator:
                assert await (await evaluator.string("1 + 1")).to_python() == 2
                raise RuntimeError("deliberate")

        assert pool.stats.retired == 1
        assert pool.stats.retired_after == [1]

        async with pool.lease():
            pass
        assert pool.stats.created == 2


async def test_capacity_bounds_live_evaluators(
    evaluator_factory: Callable[[], EvalSession],
) -> None:
    """Eight concurrent leases against two slots: never three evaluators.

    ``peak_live`` is sampled inside the pool's own lock at creation time, so it
    reflects real overlap rather than a post-hoc count.
    """
    async with pool_scope(evaluator_factory, capacity=2, max_leases=100) as pool:

        async def borrow(n: int) -> None:
            async with pool.lease() as evaluator:
                assert await (await evaluator.string(f"{n} * 2")).to_python() == n * 2

        async with anyio.create_task_group() as tg:
            for n in range(8):
                tg.start_soon(borrow, n)

        assert pool.stats.leases == 8
        assert pool.stats.peak_live <= 2
        assert pool.stats.created <= 2


async def test_aclose_retires_every_evaluator(
    evaluator_factory: Callable[[], EvalSession],
) -> None:
    """No evaluator outlives the pool -- each one is a Nix thread and a heap."""
    pool = EvalStatePool(evaluator_factory, capacity=3, max_leases=100)

    async def borrow() -> None:
        async with pool.lease() as evaluator:
            await (await evaluator.string("1")).to_python()
            await anyio.sleep(0.05)  # hold the slot so all three get created

    async with anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(borrow)

    assert pool.stats.created == 3
    assert pool.stats.retired == 0

    await pool.aclose()
    assert pool.stats.retired == pool.stats.created

    with pytest.raises(RuntimeError, match="closed"):
        async with pool.lease():
            pass


async def test_pooled_evaluator_fixture_is_usable(evaluator: EvalSession) -> None:
    """The fixture wiring itself, so a broken conftest fails here and not everywhere."""
    assert isinstance(evaluator, EvalSession)
    assert await (await evaluator.string("1 + 1")).to_python() == 2
