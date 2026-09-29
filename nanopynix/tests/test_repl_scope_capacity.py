"""The REPL scope holds a fixed number of bindings, and says so at the edge.

huggorm's ``Repl`` allocates one ``nix::Env`` for the whole REPL session and
hands out one displacement per binding. ``nix::Env::values`` is a flexible
array with no length, so the only thing standing between a 32769th binding
and a heap write past the end is an explicit bounds check. What this file pins
is the edge: filling the env exactly to the end is accepted, and one binding
more is refused.

The number below is ``env_size`` in huggorm's ``cpp/eval.hpp``. Repeated here
rather than exposed -- a caller has no use for it. If it changes, these two
tests are the reminder that the boundary they describe is something callers
can reach.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from nanopynix.exceptions import NixError

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import InprocSessionFactory, RpcSessionFactory

REPL_ENV_CAPACITY = 32768
"""``env_size`` in huggorm's ``cpp/eval.hpp``; see the module docstring."""


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
# Nix's own error and message, from `NixRepl::addAttrsToScope`, on both
# engines. It is a `nix::Error`, so it arrives as `NixError` in process and
# over the worker boundary alike.


async def test_inproc_refuses_one_binding_past_the_end(inproc_session: InprocSessionFactory) -> None:
    with pytest.raises(NixError, match="environment full; cannot add more variables"):
        await _add_attrs(inproc_session, REPL_ENV_CAPACITY + 1)


async def test_rpc_refuses_one_binding_past_the_end(rpc_session: RpcSessionFactory) -> None:
    with pytest.raises(NixError, match="environment full; cannot add more variables"):
        await _add_attrs(rpc_session, REPL_ENV_CAPACITY + 1)
