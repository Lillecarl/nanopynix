"""A pool of reusable EvalStates with a bounded lifetime.

Creating a session plus an EvalState costs ~0.140s; reusing one costs ~0.030s,
and the saving holds across *different* expressions because the nixpkgs base
stays resolved in the evaluator's value graph. Reusing one forever is not an
option either -- the value graph only grows, and nothing binds a Boehm
collection -- so an evaluator is retired after a bounded number of handouts and
a fresh one takes its slot.

**Why several evaluators rather than one shared one.** An ``EvalSession`` owns
a single dedicated Nix thread (``NixThreadExecutor(max_workers=1)``, for
``EvalState`` affinity). Handing the same evaluator to concurrent callers would
therefore funnel every evaluation onto one thread and buy no parallelism at
all. So the pool holds up to *capacity* evaluators and checks them out
exclusively: concurrency comes from the slot count, amortisation from the lease
count, and the two are tuned independently.

Exclusive checkout also gives each test an evaluator no one else is mutating,
which is the isolation property a shared evaluator could not offer.

**Why the pool does not own a Session.** ``inproc`` permits exactly one open
``Session`` per process (``_impl.py``'s ``_process_guard``), so the session and
its store are handed in already open and outlive every evaluator. That is not a
limitation here -- ``Session.eval()`` is explicitly documented to give each
evaluator an independent Nix evaluator, which is precisely the unit worth
pooling.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from nanopynix.inproc import EvalSession

# Concurrent evaluators. Measured, not guessed: on a 4-core box, 8 evaluations
# of a 300k-element fold took 0.41s at capacity 1, 0.225s at 2, and 0.248s at 4
# -- so the second evaluator is nearly free (1.83x for 4% more CPU) and the
# third and fourth burn 40% more CPU for no wall-clock gain at all. Two is the
# knee. Re-measure with test_concurrency_scales_with_capacity on other hardware
# before raising it; the numbers above are one box.
DEFAULT_CAPACITY = 2

# How many leases an evaluator serves before it is retired. Not tuned yet --
# ptest/test_pool_contract.py measures where memory actually goes, and this
# number should follow that measurement rather than intuition.
DEFAULT_MAX_LEASES = 25


@dataclass
class PoolStats:
    """What the pool did, so tests can assert on it rather than on timings."""

    created: int = 0
    retired: int = 0
    leases: int = 0
    #: Highest number of evaluators alive at the same moment.
    peak_live: int = 0
    #: Lease count each evaluator had reached when retired, oldest first.
    retired_after: list[int] = field(default_factory=list)


@dataclass
class _Slot:
    """One evaluator and its lease count."""

    evaluator: EvalSession
    leases: int = 0


class EvalStatePool:
    """Check out an ``EvalSession``, retiring each one every *max_leases* uses.

    Up to *capacity* evaluators exist at once; a caller that finds them all
    checked out waits. Returning an evaluator that has served its lease budget
    closes it, and the next caller pays for a fresh one.
    """

    def __init__(
        self,
        evaluator_factory: Callable[[], EvalSession],
        *,
        capacity: int = DEFAULT_CAPACITY,
        max_leases: int = DEFAULT_MAX_LEASES,
    ) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be at least 1, got {capacity}")
        if max_leases < 1:
            raise ValueError(f"max_leases must be at least 1, got {max_leases}")
        self._evaluator_factory = evaluator_factory
        self._capacity = capacity
        self._max_leases = max_leases
        self._slots = anyio.Semaphore(capacity)
        self._lock = anyio.Lock()
        self._idle: list[_Slot] = []
        self._live = 0
        self._closed = False
        self.stats = PoolStats()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def max_leases(self) -> int:
        return self._max_leases

    async def _open_slot(self) -> _Slot:
        evaluator = self._evaluator_factory()
        await evaluator.open()
        self._live += 1
        self.stats.created += 1
        self.stats.peak_live = max(self.stats.peak_live, self._live)
        return _Slot(evaluator=evaluator)

    async def _retire(self, slot: _Slot) -> None:
        """Close an evaluator and drop every reference to it.

        Dropping the references is all we can do today: nothing binds
        ``GC_gcollect``, so the Boehm heap is only reclaimed once allocation
        pressure triggers a collection. An explicit collect was tried once
        before and took the process down with a signal -- but that predates the
        thread-registration work in ``EvalSession.open``/``close``, so it is
        worth retrying rather than assuming it still crashes. If memory fails
        to settle in test_pool_contract, that binding is what to revisit.
        """
        self._live -= 1
        self.stats.retired += 1
        self.stats.retired_after.append(slot.leases)
        await slot.evaluator.close()

    async def warm(self, count: int | None = None) -> None:
        """Open evaluators up front so a caller does not pay for them mid-flight.

        Without this, a timed run against a cold pool creates *capacity - 1* of
        its evaluators inside the measured window, which quietly charges wider
        pools for setup the narrow ones already finished paying. It is also
        what the real suite wants at session start.
        """
        target = self._capacity if count is None else min(count, self._capacity)
        async with self._lock:
            while self._live < target:
                self._idle.append(await self._open_slot())

    @contextlib.asynccontextmanager
    async def lease(self) -> AsyncIterator[EvalSession]:
        """Borrow an evaluator for the duration of the block.

        The evaluator is returned on exit, and retired instead if it has used
        up its lease budget. An exception inside the block retires it too: we
        cannot tell a failed assertion from an evaluator left in a bad state,
        and guessing wrong contaminates the next test.
        """
        if self._closed:
            raise RuntimeError("pool is closed")
        await self._slots.acquire()
        try:
            async with self._lock:
                slot = self._idle.pop() if self._idle else await self._open_slot()
                slot.leases += 1
                self.stats.leases += 1
            failed = False
            try:
                yield slot.evaluator
            except BaseException:
                failed = True
                raise
            finally:
                async with self._lock:
                    if failed or self._closed or slot.leases >= self._max_leases:
                        await self._retire(slot)
                    else:
                        self._idle.append(slot)
        finally:
            self._slots.release()

    async def aclose(self) -> None:
        """Retire every idle evaluator. Checked-out ones retire on return."""
        async with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for slot in idle:
            async with self._lock:
                await self._retire(slot)


@contextlib.asynccontextmanager
async def pool_scope(
    evaluator_factory: Callable[[], EvalSession],
    *,
    capacity: int = DEFAULT_CAPACITY,
    max_leases: int = DEFAULT_MAX_LEASES,
) -> AsyncIterator[EvalStatePool]:
    """``EvalStatePool`` as an async context, for fixtures and tests to use."""
    pool = EvalStatePool(evaluator_factory, capacity=capacity, max_leases=max_leases)
    try:
        yield pool
    finally:
        await pool.aclose()
