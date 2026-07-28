"""The realistic benchmark: evaluate real nixpkgs packages, and time the wall clock.

Everything else here measures a microbenchmark, and microbenchmarks led this
investigation astray twice. A CPU-bound ``foldl'`` says the process is
CPU-bound; the actual suite runs at **1.2 of 4 cores** with a profiled test
spending 13.7s of 14.26s in ``epoll.poll``. Synthetic ``builtins.toFile``
writes say the store is a write-throughput problem; real evaluation interleaves
parsing, forcing, ``.drv`` writes and store queries in a pattern no synthetic
loop reproduces.

So this module does the real thing: force ``drvPath`` on a pre-selected sample
of nixpkgs packages (``nixpkgs_sample.json``, 1000 names strided evenly across
all 24915 top-level derivations, so it is representative rather than
alphabetical). Forcing ``drvPath`` instantiates, which writes a ``.drv`` per
derivation and its whole input closure -- evaluation and store traffic mixed
the way the suite actually mixes them, with no building and no substitution.

**Wall clock is the metric.** CPU is recorded only to explain a wall-clock
result, never as the result itself.

Each configuration gets a **fresh store root** and evaluates the **same**
packages, because a store warmed by the previous configuration is exactly the
contamination that made every earlier comparison here unreliable. That makes
this slow and opt-in::

    pytest ptest/test_nixpkgs_workload.py -m benchmark
    NANOPYNIX_PTEST_PACKAGES=200 pytest ptest/test_nixpkgs_workload.py -m benchmark
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest
from pytest_agent import note

import nanopynix
from ptest._cpu import tree_cpu_seconds
from ptest._daemon import daemon_for

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = [pytest.mark.concurrency, pytest.mark.benchmark]

SAMPLE_PATH = Path(__file__).parent / "nixpkgs_sample.json"
PACKAGES_ENV_VAR = "NANOPYNIX_PTEST_PACKAGES"
DEFAULT_PACKAGES = 500

# The full engine x backend x width matrix. An earlier version of this file
# omitted the inproc/daemon cells, on the reasoning that inproc against a
# daemon URI is "the same client code against a different URI". That was wrong,
# and it left the daemon result uninterpretable: rpc+daemon being slower than
# rpc+local confounds the daemon protocol with the RPC wire, and only the
# inproc+daemon cell separates them. Never drop a cell from a 2x2 because one
# axis looks uninteresting -- that is how a confound gets published as a
# finding.
CONFIGS = [
    (engine, backend, workers)
    for engine in ("inproc", "rpc")
    for backend in ("local", "daemon")
    for workers in (1, 2, 4)
]


def _sample(count: int) -> list[str]:
    names: list[str] = json.loads(SAMPLE_PATH.read_text())
    return names[:count]


def _shard_expr(names: Sequence[str]) -> str:
    """Force ``drvPath`` for each name, counting the ones that resolve.

    ``tryEval`` per package rather than around the whole list: nixpkgs always
    contains attributes that throw on this platform (unsupported systems,
    unfree without a flag), and one of them must not take the shard with it.
    Counting rather than returning the paths keeps the RPC response small, so
    what is timed is evaluation and not JSON transport.
    """
    listed = " ".join(f'"{name}"' for name in names)
    return (
        "let pkgs = import <nixpkgs> {}; in "
        "builtins.length (builtins.filter (x: x != null) (map (n: "
        "let r = builtins.tryEval pkgs.${n}.drvPath; in if r.success then r.value else null"
        f") [ {listed} ]))"
    )


def _shards(names: Sequence[str], count: int) -> list[Sequence[str]]:
    """Round-robin, not contiguous slices: package cost is wildly uneven and
    alphabetical blocks would hand one worker all the heavy ones."""
    return [names[index::count] for index in range(count)]


@pytest.mark.parametrize(("engine", "backend", "workers"), CONFIGS, ids=lambda v: str(v))
async def test_nixpkgs_evaluation_throughput(
    tmp_path_factory: pytest.TempPathFactory,
    nix_settings: nanopynix.NixSettings,
    engine: str,
    backend: str,
    workers: int,
) -> None:
    """Evaluate the sample across *workers* evaluators, from a cold store."""
    count = int(os.environ.get(PACKAGES_ENV_VAR, DEFAULT_PACKAGES))
    names = _sample(count)
    shards = _shards(names, workers)
    root = tmp_path_factory.mktemp(f"nixpkgs-{engine}-{backend}-{workers}")
    resolved: dict[int, int] = {}

    async with contextlib.AsyncExitStack() as stack:
        if backend == "daemon":
            store_uri = await stack.enter_async_context(daemon_for(root))
        else:
            store_uri = f"local://?root={root}"

        evaluators: list[object] = []
        if engine == "inproc":
            session = await stack.enter_async_context(
                nanopynix.inproc.Session(store_uri=store_uri, load_config=False, settings=nix_settings),
            )
            store = await stack.enter_async_context(session.store())
            for _ in range(workers):
                evaluators.append(await stack.enter_async_context(session.eval(store)))
        else:
            for _ in range(workers):
                rpc_session = await stack.enter_async_context(
                    nanopynix.rpc.Session(store_uri=store_uri, load_config=False, settings=nix_settings),
                )
                rpc_store = await stack.enter_async_context(rpc_session.store())
                evaluators.append(await stack.enter_async_context(rpc_session.eval(rpc_store)))

        async def run(index: int) -> None:
            evaluator = evaluators[index]
            value = await evaluator.string(_shard_expr(shards[index]))  # type: ignore[attr-defined] -- inproc and rpc EvalSession are duck-identical here, and typing the union costs more than it explains in a prototype
            result = await value.to_python()
            if not isinstance(result, int):
                raise TypeError(f"expected int, got {type(result).__name__}")
            resolved[index] = result

        started = time.monotonic()
        cpu_started = tree_cpu_seconds()
        async with anyio.create_task_group() as task_group:
            for index in range(workers):
                task_group.start_soon(run, index)
        elapsed = time.monotonic() - started
        cpu = tree_cpu_seconds() - cpu_started

    total_resolved = sum(resolved.values())
    # Most of the sample resolves; the rest throw for platform or licence
    # reasons. A collapse well below that means the shards did not run, not
    # that nixpkgs changed.
    assert total_resolved > count * 0.5, f"only {total_resolved}/{count} packages resolved"

    note(
        engine=engine,
        backend=backend,
        workers=workers,
        packages=count,
        resolved=total_resolved,
        wall=f"{elapsed:.2f}s",
        pkgs_per_second=f"{count / elapsed:.1f}",
        tree_cpu=f"{cpu:.2f}s",
        cpu_per_wall=f"{cpu / elapsed:.2f}",
    )
