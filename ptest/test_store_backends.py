"""Local store versus one shared daemon, under concurrent store *writes*.

Everything else in this directory measures evaluation, and deliberately uses an
expression that touches no store at all -- which is why the store URI has been
orthogonal to every number so far. This module is the opposite: the expression
does almost no evaluating and writes as hard as it can.

``builtins.toFile`` adds a path to the store during evaluation. Giving every
task unique content makes every write a real one, with no dedup short-circuit
and no dependence on what earlier tests happened to leave behind -- the
isolation problem that has bitten every other measurement here.

The question: N workers writing to one local store root each hold their own
``LocalStore`` and their own SQLite connection, whereas N workers against one
daemon funnel through a single process. Nix's own answer is that the daemon
serialises safely, not that it serialises *cheaply*, and for test-suite design
the difference decides whether a shared daemon is a convenience or a bottleneck.
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
from ptest._daemon import daemon_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from nanopynix.rpc import EvalSession as RpcEvalSession

pytestmark = pytest.mark.concurrency

TASKS = 8
WRITES_PER_TASK = 40

# A run-unique prefix so no write in this process can collide with, or be
# deduplicated against, one from an earlier run against a persisted store.
RUN_TAG = f"{os.getpid()}-{time.time_ns()}"


def _write_expr(task: int) -> str:
    """Evaluate to a list of freshly-added store paths, and nothing else."""
    return (
        f"builtins.genList (i: builtins.toFile "
        f'"ptest-{RUN_TAG}-{task}-${{toString i}}" '
        f'"content {RUN_TAG} {task} ${{toString i}}") {WRITES_PER_TASK}'
    )


async def _write(evaluator: RpcEvalSession, task: int) -> int:
    paths = await (await evaluator.string(_write_expr(task))).to_python()
    if not isinstance(paths, list):
        raise TypeError(f"expected list, got {type(paths).__name__}")
    return len(paths)


@pytest.fixture(scope="session")
async def daemon_uri(store_root: Path) -> AsyncIterator[str]:
    """One daemon over the same root the local-store tests use."""
    async with daemon_for(store_root) as uri:
        yield uri


@pytest.mark.parametrize("workers", [1, 2, 4])
@pytest.mark.parametrize("backend", ["local", "daemon"])
async def test_store_writes_scale_by_backend(
    store_root: Path,
    nix_settings: nanopynix.NixSettings,
    daemon_uri: str,
    backend: str,
    workers: int,
) -> None:
    """320 store additions spread over *workers* RPC workers, per backend.

    RPC rather than inproc because the earlier measurement showed process
    isolation is what actually scales; using it here keeps the store as the
    only variable.

    Recorded, not asserted. The assertion is that every write lands: a store
    that silently dropped or collided writes under concurrency would fail here
    regardless of how fast it was.
    """
    store_uri = f"local://?root={store_root}" if backend == "local" else daemon_uri
    written: dict[int, int] = {}

    async with contextlib.AsyncExitStack() as stack:
        evaluators: list[RpcEvalSession] = []
        for _ in range(workers):
            session = await stack.enter_async_context(
                nanopynix.rpc.Session(store_uri=store_uri, load_config=False, settings=nix_settings),
            )
            store = await stack.enter_async_context(session.store())
            evaluators.append(await stack.enter_async_context(session.eval(store)))

        # Warm each evaluator so parsing and base-env setup are not in the clock.
        async with anyio.create_task_group() as tg:
            for index, evaluator in enumerate(evaluators):
                tg.start_soon(_write, evaluator, 1000 + index)

        async def run(task: int) -> None:
            written[task] = await _write(evaluators[task % len(evaluators)], task)

        started = time.monotonic()
        cpu_started = tree_cpu_seconds()
        async with anyio.create_task_group() as tg:
            for task in range(TASKS):
                tg.start_soon(run, task)
        elapsed = time.monotonic() - started
        cpu = tree_cpu_seconds() - cpu_started

    assert written == {task: WRITES_PER_TASK for task in range(TASKS)}

    total = TASKS * WRITES_PER_TASK
    note(
        backend=backend,
        workers=workers,
        writes=total,
        wall=f"{elapsed:.3f}s",
        tree_cpu=f"{cpu:.3f}s",
        cpu_per_wall=f"{cpu / elapsed:.2f}",
        writes_per_second=f"{total / elapsed:.0f}",
    )
