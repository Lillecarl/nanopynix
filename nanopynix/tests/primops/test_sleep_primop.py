"""``builtins.sleep``, the one builtin nanopynix adds that upstream Nix lacks.

It exists so that a test can make an evaluation last a *known* time. The cancel
tests need an operation that Nix cannot interrupt, and they used to get one
from a large fold -- which measures the machine rather than stating a duration,
and which therefore passed on one macOS host and failed on another.

``nanopynix-bindings/src/nix_expr.cpp`` implements it and carries the reasoning.
These tests pin the three properties that reasoning depends on.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from nanopynix.exceptions import EvalError

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import InprocSessionFactory

#: Long enough to tell a real wait from a rounding error, short enough to pay
#: for on every run.
_SLEEP_SECONDS = 0.4

#: How far the measured wait may fall short. A sleep may overshoot on a loaded
#: machine, and never has to undershoot, so only this side needs a bound.
_TOLERANCE = 0.05


@pytest.mark.anyio
async def test_sleep_waits_for_the_time_it_is_given(inproc_session: InprocSessionFactory) -> None:
    """The point of the builtin: the duration comes from the caller, not the machine."""
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as evaluator:
        started = time.monotonic()
        result = await evaluator.string(f"builtins.sleep {_SLEEP_SECONDS}")
        assert await result.to_python() is True
        assert time.monotonic() - started >= _SLEEP_SECONDS - _TOLERANCE


@pytest.mark.anyio
async def test_sleep_is_a_builtin_and_not_a_global(inproc_session: InprocSessionFactory) -> None:
    """Registered as ``__sleep``, so it does not shadow a binding of the caller.

    ``EvalState::addPrimOp`` puts the unstripped name into ``staticBaseEnv``, so
    a primop named ``sleep`` would also be a global identifier. Upstream draws
    the same line: ``__head`` is ``builtins.head`` alone, and ``map`` is
    deliberately both.
    """
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as evaluator:
        present = await evaluator.string("builtins ? sleep")
        assert await present.to_python() is True

        shadowed = await evaluator.string("let sleep = 42; in sleep")
        assert await shadowed.to_python() == 42


@pytest.mark.anyio
async def test_sleep_refuses_a_negative_duration(inproc_session: InprocSessionFactory) -> None:
    """A negative wait is a mistake in the caller, and silence would hide it."""
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as evaluator:
        # `string` forces to weak head normal form, so the application runs
        # here rather than at `to_python`.
        with pytest.raises(EvalError, match="not negative"):
            await evaluator.string("builtins.sleep (-1)")
