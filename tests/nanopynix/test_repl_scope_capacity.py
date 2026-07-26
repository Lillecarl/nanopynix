"""The REPL scope holds a fixed number of bindings, and says so at the edge.

``begin_repl`` allocates one ``nix::Env`` for the whole REPL session and hands
out one displacement per binding. ``nix::Env::values`` is a flexible array with
no length, so nothing about it can be checked at the point of use: the only
thing standing between a 32769th binding and a heap write past the end is an
explicit bounds check, and there are three of them, in
``repl_process_line`` (via ``repl_bind``) and ``repl_add_attrs``.

They used to spell the size literally while the allocation used a ``constexpr``
scoped inside ``begin_repl``, so changing the allocation would have left the
guards protecting the old bound -- silently, and in the direction that
corrupts. They now read ``PyEvalState::repl_env_capacity``, which is set from
the value passed to ``allocEnv``, so the two cannot disagree.

That much is structural and no test can observe it. What this file pins is the
edge itself, which *is* observable and was off by one: filling the env exactly
to the end used to be refused, because the batch check asked
``displ + size >= capacity`` where the last index used is ``displ + size - 1``.

The number below is ``begin_repl``'s ``repl_env_size``. Deliberately repeated
rather than exposed through a binding -- a caller has no use for it, and one
more entry on the public surface to save a test from naming a constant is the
wrong trade. If it changes, these two tests are the reminder that the boundary
they describe is something callers can reach.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from tests.support.nix_environment import InprocSessionFactory, RpcSessionFactory

REPL_ENV_CAPACITY = 32768
"""``repl_env_size`` in ``PyEvalState::begin_repl``; see the module docstring."""


def _attrset_of(count: int) -> str:
    """A Nix expression for an attrset with *count* distinct attributes."""
    return f'builtins.listToAttrs (builtins.genList (i: {{ name = "a" + toString i; value = i; }}) {count})'


async def _add_attrs(factory: Any, count: int) -> list[str]:
    """Merge an attrset of *count* attributes into a fresh REPL scope.

    A fresh scope each time because the capacity is per-session: the assertions
    below are about a batch measured from displacement zero, which is where
    ``begin_repl`` leaves it. (``scope_names()`` reports ~118 names there, but
    those are builtins reached through the *parent* static env, not
    displacements in this one.)
    """
    async with factory() as session, session.store() as store, session.repl(store) as repl:
        value = await repl.string(_attrset_of(count))
        return await repl.add_attrs(value)


# ── Exactly full is allowed ──────────────────────────────────────────
#
# Non-vacuous by construction: with the old `>=` check this is the case that
# raised, so both of these fail against the previous bindings.


async def test_inproc_fills_the_repl_scope_exactly_to_the_end(inproc_session: InprocSessionFactory) -> None:
    names = await _add_attrs(inproc_session, REPL_ENV_CAPACITY)
    assert len(names) == REPL_ENV_CAPACITY


async def test_rpc_fills_the_repl_scope_exactly_to_the_end(rpc_session: RpcSessionFactory) -> None:
    names = await _add_attrs(rpc_session, REPL_ENV_CAPACITY)
    assert len(names) == REPL_ENV_CAPACITY


# ── One past the end is refused, not written ─────────────────────────
#
# `RuntimeError` on both engines, and not narrowed further on purpose. inproc
# gets the bindings' own `std::runtime_error` translation; rpc gets a
# `nanopynix.NixError`, which subclasses `RuntimeError` -- so the type a caller
# writes in an `except` clause agrees, while the exact class and the message
# text do not (rpc's reads "[Unknown] RuntimeError: ..."). That difference is a
# property of every non-Nix exception crossing the worker boundary rather than
# anything about this condition, so it is recorded here and not asserted.


async def test_inproc_refuses_one_binding_past_the_end(inproc_session: InprocSessionFactory) -> None:
    with pytest.raises(RuntimeError, match="REPL environment is full"):
        await _add_attrs(inproc_session, REPL_ENV_CAPACITY + 1)


async def test_rpc_refuses_one_binding_past_the_end(rpc_session: RpcSessionFactory) -> None:
    with pytest.raises(RuntimeError, match="REPL environment is full"):
        await _add_attrs(rpc_session, REPL_ENV_CAPACITY + 1)
