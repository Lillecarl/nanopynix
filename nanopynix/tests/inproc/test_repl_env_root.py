"""The collector must not free the REPL environment while the scope is open.

``begin_repl`` allocates one ``nix::Env`` with ``GC_MALLOC`` for the whole REPL
session. Boehm scans its own heap, the stacks of the threads it knows, and the
memory it hands out as uncollectable. It scans no part of the Python heap, so a
``nix::Env *`` member of ``PyEvalState`` roots nothing at all.

That was issue #70. The collector freed the environment while the REPL was
using it, the block came back as an array of values, and ``ExprVar::eval`` read
one of those values where it expected a ``Value *``. The crash landed on a
``movdqa`` with no way back to the cause, once in about four runs of the soak.

``PyEvalState::repl_env_root`` holds it now: an ``std::allocate_shared`` with
``traceable_allocator``, which is what ``nix::EvalState`` uses for its own base
environment, and what ``nix::allocRootValue`` uses for a value.

**The behaviour cannot be tested through the public API.** A freed block keeps
its old contents until something else takes it, so reading the binding back
answers "yes" whether or not the environment is still alive. A finalizer
answers the real question, and ``NANOPYNIX_GC_REPL_ENV_PROBE`` is what puts one
on the environment.

Every test here drives its collections from a plain evaluator rather than from
the REPL, so exactly one REPL environment exists at a time and a count can only
mean that one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_bindings import expr as nanopynix_expr

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import InprocSessionFactory

CHURN = "builtins.length (builtins.genList (i: { a = i; b = toString i; }) 200000)"
"""Enough allocation that a freed block is handed to something else."""

ENV_BYTES = 8 + 32768 * 8
"""``sizeof(Env)`` plus ``repl_env_size`` slots; see ``PyEvalState::begin_repl``."""


def _collect() -> None:
    nanopynix_expr._gc_collect()


def _finalized() -> int:
    return nanopynix_expr._gc_repl_env_finalized()


def _self_test() -> tuple[int, int]:
    return (
        nanopynix_expr._gc_finalizer_self_test(200, 64),
        nanopynix_expr._gc_finalizer_self_test(20, ENV_BYTES),
    )


async def _churn(driver: Any) -> None:
    """Collect, allocate hard, and collect again. Evaluator thread only."""
    await driver.run(_collect)
    for _ in range(2):
        await driver.string(CHURN)
    await driver.run(_collect)


async def _assert_the_probe_can_fire(driver: Any) -> None:
    """The control, and every measurement below runs it first.

    A count of zero means nothing until the machinery is known to work. This
    allocates blocks that are certainly garbage, at 64 bytes and at the
    environment's own size, and asks how many finalizers ran.

    A conservative collector keeps a block that any stale stack slot still
    names, so a few survivors are correct. The bounds are loose on purpose:
    this asks whether the machinery works, not how well it works.
    """
    small, env_sized = await driver.run(_self_test)
    assert small > 150, f"only {small} of 200 small blocks were finalized"
    assert env_sized > 15, f"only {env_sized} of 20 environment-sized blocks were finalized"


# Neither test can join a soak lane, and neither needs a DENYLIST entry to say
# so: `monkeypatch` is not a fixture the soak driver supplies, and
# `_candidates_in` reads the signature against the ones it does. That is the
# mechanical rule doing the right thing, and it is worth naming here because
# dropping the parameter would silently put a test that forces collections and
# counts process-wide finalizers beside seven peers.


async def test_the_repl_environment_survives_a_collection(
    inproc_session: InprocSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No REPL environment dies while its scope is open. This is issue #70.

    Measured against the unrooted bindings, with one environment in flight:
    an idle scope lost it, and so did a scope that had evaluated once.
    """
    monkeypatch.setenv("NANOPYNIX_GC_REPL_ENV_PROBE", "1")
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as driver:
        await _assert_the_probe_can_fire(driver)
        before = await driver.run(_finalized)

        async with nix.repl(store) as repl:
            assert await repl.line("answer = 42") is None
            await _churn(driver)
            during = await driver.run(_finalized)
            assert during == before, "the collector freed the REPL environment while the scope was open"

            # The binding still reads, which is what a caller would notice.
            value = await repl.line("answer")
            if value is None:
                raise AssertionError("REPL expression unexpectedly created a binding")
            assert await value.as_int() == 42


async def test_the_repl_environment_is_released_when_the_scope_closes(
    inproc_session: InprocSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The root must not turn the environment into a leak.

    ``traceable_allocator`` gives memory that Boehm never collects, so a root
    that outlives its scope would hold 256 KiB for the life of the process. The
    root is a member, and closing the scope destroys the evaluator that owns
    it, so the environment becomes collectable again.
    """
    monkeypatch.setenv("NANOPYNIX_GC_REPL_ENV_PROBE", "1")
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as driver:
        await _assert_the_probe_can_fire(driver)
        before = await driver.run(_finalized)

        async with nix.repl(store) as repl:
            assert await repl.line("answer = 42") is None

        await _churn(driver)
        assert await driver.run(_finalized) > before, "the REPL environment outlived its scope, so the root leaks it"
