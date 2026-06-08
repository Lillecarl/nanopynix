#! /usr/bin/env python3
"""Self-contained test harness for the goal infrastructure.

Spins up a LocalSocketStore, evaluates derivations with nix,
and exercises the full goal tree (BuildGoal + ResolutionGoal)
without the complexity of the full pynixd server.

Usage:
    ./tmp/test_goals.py                          # run all tests
    ./tmp/test_goals.py test_simple_build        # named test
    ./tmp/test_goals.py test_resolution --trace  # verbose logging

Add --trace for structlog debug output, otherwise only warnings+ are shown.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from subprocess import PIPE

import structlog
from anyio import TemporaryDirectory

# ── Logging ───────────────────────────────────────────────────────
# Silenced by default.  Pass --trace to see everything.

_LEVEL = logging.WARNING if "--trace" not in sys.argv else logging.NOTSET


def _setup_logging(level: int = _LEVEL) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


log = structlog.get_logger()


# ── Test helpers ───────────────────────────────────────────────────


async def make_store(td: str):
    """Create a LocalSocketStore with a temp directory."""
    from pynixd.config import LocalSocketStoreSpec
    from pynixd.store import LocalSocketStore
    from pynixd.system_features import KNOWN_FEATURES
    from pynixd.types import StoreId

    store = LocalSocketStore(
        LocalSocketStoreSpec(
            store_id=StoreId("local"),
            feature_matrix={"x86_64-linux": set(KNOWN_FEATURES)},
            probe=False,
            gc_enabled=False,
            store_path=Path(td),
            use_db=True,
        )
    )
    await store.start()
    return store


async def make_context(store):
    """Create a GoalContext with store + substitution manager."""
    from pynixd.goals.goal import GoalContext
    from pynixd.goals.manager import GoalManager
    from pynixd.substitution import (
        HttpBinaryCacheSubstituter,
        SubstitutionManager,
    )

    return GoalContext(
        goal_manager=GoalManager(),
        store=store,
        substitution_manager=SubstitutionManager(
            substituters=[HttpBinaryCacheSubstituter("https://cache.nixos.org")],
        ),
    )


async def nix_drv(store_path: str, expr: str) -> str:
    """Evaluate a Nix expression and return the .drv path string."""
    proc = await asyncio.create_subprocess_exec(
        "nix",
        "eval",
        "--store",
        store_path,
        "--impure",
        "--expr",
        expr,
        "--raw",
        stdout=PIPE,
        stderr=PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"nix eval failed: {stderr.decode()}")
    return stdout.decode().splitlines()[0].strip()


async def build_drv(store, ctx, drv_str: str, output_name: str = "out"):
    """Build a single derivation output through the goal tree.

    Returns the GoalResult.
    """
    from pynixd.derived_path import DerivedPath
    from pynixd.goals.goal import make_build_goal

    dp = DerivedPath(f"{drv_str}!{output_name}")
    goal = make_build_goal(dp, ctx)
    await goal.run()
    return goal.result


async def resolve_output(ctx, drv_path: str, output_name: str = "out"):
    """Resolve a single derivation output through the resolution goal tree.

    Returns the GoalResult.
    """
    from pynixd.goals.goal import make_resolution_goal
    from pynixd.store_path import StorePath

    goal = make_resolution_goal(StorePath(drv_path), output_name, ctx)
    registered = ctx.goal_manager.register(goal)
    await registered.run()
    return registered.result


# ── Tests ──────────────────────────────────────────────────────────


async def test_simple_build() -> None:
    """Build pkgs.hello via the goal tree and verify the output path."""
    log.info("test_simple_build", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_drv(
            td,
            "let pkgs = import <nixpkgs> {}; in pkgs.hello.drvPath",
        )
        log.info("test_simple_build", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )
        assert result.produced_paths, "no produced paths"
        log.info("test_simple_build", status=result.result.status, paths=result.produced_paths)

        # Verify the output is valid in the store
        from pynixd.operations.is_valid_path import IsValidPathRequest

        for sp in result.produced_paths:
            valid = (await store.execute(IsValidPathRequest(path=sp))).valid
            assert valid, f"produced path {sp} is not valid"

        log.info("test_simple_build", msg="PASSED")
        await store.close()


async def test_resolution() -> None:
    """Test that ResolutionGoal resolves outputs for a fixed derivation."""
    log.info("test_resolution", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_drv(
            td,
            "let pkgs = import <nixpkgs> {}; in pkgs.hello.drvPath",
        )
        log.info("test_resolution", drv=drv_str)

        # Resolve the "out" output via ResolutionGoal
        result = await resolve_output(ctx, drv_str, "out")
        assert result is not None, "resolution returned None"
        assert result.resolved_outputs, f"no resolved outputs: {result}"
        out_path = result.resolved_outputs.get("out")
        assert out_path is not None, f"out not in resolved_outputs"
        log.info("test_resolution", out_path=str(out_path))

        # For a fixed derivation, the path should be from the .drv, not computed
        assert not result.modulo_hash, "fixed derivation should not have modulo_hash"

        log.info("test_resolution", msg="PASSED")
        await store.close()


async def test_deferred_resolution() -> None:
    """Exercise the resolution + build pipeline end-to-end."""
    log.info("test_deferred_resolution", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_drv(
            td,
            "let pkgs = import <nixpkgs> {}; in pkgs.hello.drvPath",
        )
        log.info("test_deferred_resolution", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "build result is None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )
        log.info("test_deferred_resolution", status=result.result.status, paths=result.produced_paths)

        log.info("test_deferred_resolution", msg="PASSED")
        await store.close()


async def test_opaque_path() -> None:
    """Resolve an opaque store path via the goal tree."""
    log.info("test_opaque_path", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        from pynixd.derived_path import DerivedPath
        from pynixd.goals.goal import make_build_goal

        # Build hello first so we have a known path
        drv_str = await nix_drv(
            td,
            "let pkgs = import <nixpkgs> {}; in pkgs.hello.drvPath",
        )
        build_result = await build_drv(store, ctx, drv_str, "out")
        assert build_result is not None and build_result.produced_paths
        hello_path = list(build_result.produced_paths)[0]

        # Create an opaque goal for the path (should be ALREADY_VALID)
        goal = make_build_goal(DerivedPath(str(hello_path)), ctx)
        await goal.run()
        assert goal.result is not None, "opaque goal returned None"
        assert goal.result.result.status == 2, (
            f"expected ALREADY_VALID (2), got {goal.result.result.status}"
        )

        log.info("test_opaque_path", msg="PASSED")
        await store.close()


# ── main ───────────────────────────────────────────────────────────

_TESTS = {
    "test_simple_build": test_simple_build,
    "test_resolution": test_resolution,
    "test_deferred_resolution": test_deferred_resolution,
    "test_opaque_path": test_opaque_path,
}


async def async_main() -> None:
    _setup_logging()

    requested = [a for a in sys.argv[1:] if not a.startswith("--")]
    tests = [t for name, t in _TESTS.items() if not requested or name in requested]

    if not tests:
        print(f"Available tests: {', '.join(_TESTS)}", file=sys.stderr)
        sys.exit(1)

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            await test_fn()
            passed += 1
        except Exception as e:
            log.exception(f"{test_fn.__name__} FAILED", error=str(e))
            failed += 1

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
