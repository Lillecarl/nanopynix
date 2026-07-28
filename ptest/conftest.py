"""Fixtures for the 2.0 prototype: one process Session, one warm store, pooled evaluators.

Deliberately flat compared to tests/. The shape is not a preference, it is what
``inproc`` permits: exactly one ``Session`` may be open per process
(``_impl.py``'s ``_process_guard``), so the session is a session-scoped
singleton and everything else hangs off it. What *can* multiply is the
evaluator -- ``Session.eval()`` hands out an independent Nix evaluator with its
own thread -- so that is the thing the pool pools.

Anything a test needs beyond this should force a decision about the isolation
contract in README.md rather than quietly adding a tier.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from nanopynix_bindings import util as nanopynix_util

import nanopynix
from ptest._pool import DEFAULT_CAPACITY, DEFAULT_MAX_LEASES, EvalStatePool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from nanopynix.inproc import EvalSession, Session, Store

# Kept across runs when NANOPYNIX_PTEST_STORE points somewhere: a cold store
# roughly doubles evaluation time, because evaluating instantiates and every
# .drv write costs ~0.6ms. Reusing a populated store is the cheapest way to
# stop paying that, and unlike the old fixtures this one says so out loud.
STORE_ENV_VAR = "NANOPYNIX_PTEST_STORE"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Session-scoped so one event loop serves the session fixtures and the tests.

    A function-scoped backend would give the ``Session`` a different loop from
    the tests using it, and its log-forwarding task lives on the loop that
    opened it.
    """
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def _libstore() -> None:
    """Process-global Nix configuration. One per process, by construction."""
    nanopynix_util.set_setting("build-users-group", "")
    nanopynix_util.set_setting("require-drop-supplementary-groups", "false")
    nanopynix.init_libstore(load_config=False)


@pytest.fixture(scope="session")
def store_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The one store every test shares, warm across runs when asked.

    No teardown when persisted: that is the point. When ephemeral, pytest's own
    tmp retention handles it -- and note a SIGKILLed run cleans up neither, an
    unsolved problem the real suite has already hit (9.8 GB leaked in one day).
    """
    configured = os.environ.get(STORE_ENV_VAR)
    if configured:
        root = Path(configured)
        root.mkdir(parents=True, exist_ok=True)
        yield root
        return
    yield tmp_path_factory.mktemp("ptest-store")


@pytest.fixture(scope="session")
def nix_settings() -> nanopynix.NixSettings:
    return nanopynix.NixSettings(
        build_users_group="",
        require_drop_supplementary_groups=False,
        # Substitute from the host daemon first; this is the one host coupling
        # these otherwise-hermetic stores intentionally keep.
        substituters=["daemon", "https://cache.nixos.org"],
    )


@pytest.fixture(scope="session")
async def nix_session(
    store_root: Path,
    nix_settings: nanopynix.NixSettings,
    _libstore: None,
) -> AsyncIterator[Session]:
    """The process's one and only ``inproc.Session``."""
    del _libstore
    async with nanopynix.inproc.Session(
        store_uri=f"local://?root={store_root}",
        load_config=False,
        settings=nix_settings,
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def nix_store(nix_session: Session) -> AsyncIterator[Store]:
    """The shared warm store. Concurrent access is libstore's problem, and it handles it."""
    async with nix_session.store() as store:
        yield store


@pytest.fixture(scope="session")
def evaluator_factory(nix_session: Session, nix_store: Store) -> Callable[[], EvalSession]:
    """Make an unopened evaluator against the shared store.

    Unopened because opening is the expensive part, and the pool wants to
    decide when it happens.
    """

    def make() -> EvalSession:
        return nix_session.eval(nix_store)

    return make


@pytest.fixture(scope="session")
async def eval_pool(evaluator_factory: Callable[[], EvalSession]) -> AsyncIterator[EvalStatePool]:
    """The pooled evaluators shared by every test that only needs to evaluate."""
    pool = EvalStatePool(evaluator_factory, capacity=DEFAULT_CAPACITY, max_leases=DEFAULT_MAX_LEASES)
    try:
        yield pool
    finally:
        await pool.aclose()


@pytest.fixture
async def evaluator(eval_pool: EvalStatePool) -> AsyncIterator[EvalSession]:
    """A possibly-reused EvalSession, checked out for this test alone.

    Exclusive for the duration of the test, so concurrent tests get different
    evaluators -- an EvalSession has one Nix thread, and sharing one would
    serialise every evaluation in the run onto it.
    """
    async with eval_pool.lease() as evaluator:
        yield evaluator


@pytest.fixture
async def private_evaluator(evaluator_factory: Callable[[], EvalSession]) -> AsyncIterator[EvalSession]:
    """A fresh evaluator, closed afterwards.

    For the tests that genuinely cannot share one: they mutate files on disk,
    or they assert on the evaluator's own caching behaviour. Asking for it
    should be a considered choice, since it forfeits the amortisation.
    """
    async with evaluator_factory() as evaluator:
        yield evaluator
