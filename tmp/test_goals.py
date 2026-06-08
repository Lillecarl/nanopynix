#! /usr/bin/env python3
"""Self-contained test harness for the goal infrastructure.

Spins up a LocalSocketStore, evaluates derivations with nix,
and exercises the full goal tree (BuildGoal + ResolutionGoal)
without the complexity of the full pynixd server.

Usage:
    ./tmp/test_goals.py                              # run all tests
    ./tmp/test_goals.py test_deferred                # named test
    ./tmp/test_goals.py test_simple_build --trace     # verbose logging

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

_HERE = Path(__file__).resolve().parent
_TEST_NIX = (_HERE / "../tests/nix/default.nix").resolve()

# ── Logging ───────────────────────────────────────────────────────

_LEVEL = logging.WARNING if "--trace" not in sys.argv else logging.NOTSET

_SILENCED_EVENTS = frozenset({
    # Store/daemon connection management (not goal-related)
    "spawning_managed_daemon",
    "daemon_stderr",
    "daemon_protocol_negotiated",
    "daemon_nix_version",
    "daemon_socket_ready",
    "resource_poller_started",
    "terminating_daemon_process_group",
    "pool_reusing_conn",
    "store_paths_synced",
    "pool_created_connection",
    "daemon_features",
    "connecting_daemon_socket",
    "store_discarding_dirty_connection",
    "build_derivation_timing",
})


def _drop_silenced(logger, method_name, event_dict):
    event = event_dict.get("event", "")
    if event in _SILENCED_EVENTS:
        raise structlog.DropEvent
    return event_dict


def _setup_logging(level: int = _LEVEL) -> None:
    structlog.configure(
        processors=[
            _drop_silenced,
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


async def nix_eval(store_path: str, attr: str) -> str:
    """Evaluate a Nix attribute and return its output (.drvPath or path)."""
    proc = await asyncio.create_subprocess_exec(
        "nix",
        "eval",
        "--store",
        store_path,
        "--impure",
        "--file",
        str(_TEST_NIX),
        attr,
        "--raw",
        stdout=PIPE,
        stderr=PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"nix eval {attr} failed: {stderr.decode()}")
    return stdout.decode().splitlines()[0].strip()


async def build_drv(store, ctx, drv_str: str, output_name: str = "out"):
    """Build a single derivation output through the goal tree."""
    from pynixd.derived_path import DerivedPath
    from pynixd.goals.goal import make_build_goal

    dp = DerivedPath(f"{drv_str}!{output_name}")
    goal = make_build_goal(dp, ctx)
    await goal.run()
    return goal.result


# ── Tests ──────────────────────────────────────────────────────────


async def test_simple_build() -> None:
    """Build a simple (non-CA) derivation via the goal tree."""
    log.info("test_simple_build", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        # Use ``dyn.hello`` (defined in dyn-drv.nix, no nixpkgs dependency)
        # instead of ``pkgs.hello`` which requires nixpkgs in the store.
        drv_str = await nix_eval(td, "dyn.hello.drvPath")
        log.info("test_simple_build", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )
        assert result.produced_paths, "no produced paths"

        from pynixd.operations.is_valid_path import IsValidPathRequest
        for sp in result.produced_paths:
            valid = (await store.execute(IsValidPathRequest(path=sp))).valid
            assert valid, f"produced path {sp} is not valid"

        log.info("test_simple_build", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


async def test_ca_simple() -> None:
    """Build a CA floating derivation (ca.simple)."""
    log.info("test_ca_simple", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_eval(td, "ca.simple.drvPath")
        log.info("test_ca_simple", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )
        assert result.produced_paths, "no produced paths"

        from pynixd.operations.is_valid_path import IsValidPathRequest
        for sp in result.produced_paths:
            valid = (await store.execute(IsValidPathRequest(path=sp))).valid
            assert valid, f"CA output {sp} is not valid"

        log.info("test_ca_simple", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


async def test_ca_multi_output() -> None:
    """Build a CA derivation with multiple outputs (ca.multi_output)."""
    log.info("test_ca_multi_output", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_eval(td, "ca.multi_output.drvPath")
        log.info("test_ca_multi_output", drv=drv_str)

        for out_name in ("out", "dev"):
            result = await build_drv(store, ctx, drv_str, out_name)
            assert result is not None, f"no result for {out_name}"
            assert result.result.status in (0, 1, 2, 13), (
                f"build of {out_name} failed: {result.result.status} {result.result.error_msg}"
            )
            assert result.produced_paths, f"no produced paths for {out_name}"
            log.info("test_ca_multi_output", output=out_name, paths=result.produced_paths)

        log.info("test_ca_multi_output", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


async def test_ca_depends_on_ca() -> None:
    """Build a CA derivation that depends on another CA (ca.depends_on_ca)."""
    log.info("test_ca_depends_on_ca", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_eval(td, "ca.depends_on_ca.drvPath")
        log.info("test_ca_depends_on_ca", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )
        assert result.produced_paths, "no produced paths"

        log.info("test_ca_depends_on_ca", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


async def test_deferred_non_ca_depends_on_ca() -> None:
    """Build a non-CA (deferred) derivation that depends on a CA derivation.

    This exercises the DEFERRED resolution path in ResolutionGoal:
    1. The non-CA derivation's .drv has path=\"\" (deferred outputs)
    2. ResolutionGoal must compute hashDerivationModulo and derive paths
    3. Input dep (CA simple) must be built first
    4. Placeholders must be rewritten with the actual CA output path
    """
    log.info("test_deferred", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        # Build the CA dependency explicitly first so its realisations
        # are registered in the store, then build the non-CA deferred
        # derivation that depends on it.
        ca_drv = await nix_eval(td, "ca.simple.drvPath")
        ca_result = await build_drv(store, ctx, ca_drv, "out")
        assert ca_result is not None and ca_result.result.status in (0, 1, 2, 13), (
            f"CA dep build failed"
        )
        log.info("test_deferred", ca_built=ca_result.produced_paths)

        # The GoalManager was cleared by build_paths. Now evaluate and
        # build non_ca_depends_on_ca in a separate goal tree.
        drv_str = await nix_eval(td, "ca.non_ca_depends_on_ca.drvPath")
        log.info("test_deferred", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"deferred build failed: {result.result.status} {result.result.error_msg}"
        )
        assert result.produced_paths, "no produced paths"

        # Verify the output contains the CA content
        for sp in result.produced_paths:
            fs_path = store.store_path / str(sp).lstrip("/")
            if fs_path.exists() and fs_path.is_file():
                content = fs_path.read_text()
                assert "dep-on-" in content, f"unexpected content in {sp}: {content[:200]}"
                log.info("test_deferred", path=sp, content_preview=content.strip()[:80])

        log.info("test_deferred", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


async def test_ca_fixed() -> None:
    """Build a fixed-output CA derivation (known content hash at eval time).

    Unlike floating CA (outputHash=""), a fixed-output CA has a known
    content hash at evaluation time, so the .drv contains the expected
    output path.  The daemon builds it, verifies the hash, and registers
    the realisation.

    The ResolutionGoal hits the CA_FIXED path and returns the known
    output path immediately — no hashDerivationModulo needed.
    """
    log.info("test_ca_fixed", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_eval(td, "ca.fixed_ca.drvPath")
        log.info("test_ca_fixed", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )
        assert result.produced_paths, "no produced paths"

        from pynixd.operations.is_valid_path import IsValidPathRequest
        for sp in result.produced_paths:
            valid = (await store.execute(IsValidPathRequest(path=sp))).valid
            assert valid, f"CA fixed output {sp} is not valid"

        log.info("test_ca_fixed", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


async def test_ca_text_hashed() -> None:
    """Build a text-hashed CA derivation (ca.text_hashed)."""
    log.info("test_ca_text_hashed", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_eval(td, "ca.text_hashed.drvPath")
        log.info("test_ca_text_hashed", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )

        log.info("test_ca_text_hashed", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


async def test_dyn_hello() -> None:
    """Build dyn.hello (simple regular derivation in dyn set)."""
    log.info("test_dyn_hello", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_eval(td, "dyn.hello.drvPath")
        log.info("test_dyn_hello", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )

        log.info("test_dyn_hello", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


async def test_dyn_producing_drv() -> None:
    """Build dyn.producingDrv (CA that outputs a .drv file).

    This is the first step in the dynamic derivation chain.
    """
    log.info("test_dyn_producing_drv", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_eval(td, "dyn.producingDrv.drvPath")
        log.info("test_dyn_producing_drv", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )
        assert result.produced_paths, "no produced paths"

        # The output should be a .drv file (text-hashed CA copies the .drv)
        for sp in result.produced_paths:
            log.info("test_dyn_producing_drv", path=sp, is_derivation=sp.is_derivation())

        log.info("test_dyn_producing_drv", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


async def test_dyn_wrapper() -> None:
    """Build dyn.wrapper — the full dynamic derivation chain.

    This exercises the DynamicBuildGoal (nested DerivedPath):
    1. Build producingDrv → produces a .drv file as output
    2. Read the inner .drv from the result
    3. Build the inner derivation
    4. Build wrapper which references the inner output via outputOf
    """
    log.info("test_dyn_wrapper", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_eval(td, "dyn.wrapper.drvPath")
        log.info("test_dyn_wrapper", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )

        log.info("test_dyn_wrapper", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


async def test_deep_dynamic() -> None:
    """Build a derivation with 5 layers of nested outputOf.

    builtins.outputOf wraps its first argument in a
    SingleDerivedPath::Built, which can be chained arbitrarily deep.
    The outermost wrapper's .drv has a dynamic_input_drvs entry with
    5 levels of childMap nesting.

    This exercises:
    1. The Nix evaluator producing 5-deep DownstreamPlaceholder chain
    2. pynixd's resolve_deferred with deep dynamic_input_drvs
    3. The daemon resolving 5 levels of placeholder indirection
    """
    log.info("test_deep_dynamic", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_eval(td, "dyn.deepWrapper.drvPath")
        log.info("test_deep_dynamic", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )

        log.info("test_deep_dynamic", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


async def test_crazy_mixed_deps() -> None:
    """Build a derivation that mixes regular, CA, and dynamic deps.

    Exercises:
    1. Multiple dynamic_input_drvs entries with different chain depths
    2. Mixed regular + CA + dynamic deps in one .drv
    3. Dynamic chains of different lengths running in parallel
    """
    log.info("test_crazy_mixed_deps", msg="starting")
    async with TemporaryDirectory() as td:
        store = await make_store(td)
        ctx = await make_context(store)

        drv_str = await nix_eval(td, "dyn.crazy.drvPath")
        log.info("test_crazy_mixed_deps", drv=drv_str)

        result = await build_drv(store, ctx, drv_str, "out")
        assert result is not None, "goal returned None"
        assert result.result.status in (0, 1, 2, 13), (
            f"build failed: {result.result.status} {result.result.error_msg}"
        )

        # Verify output contains content from all dependency types
        for sp in (result.produced_paths or set()):
            fs_path = store.store_path / str(sp).lstrip("/")
            if fs_path.exists() and fs_path.is_file():
                content = fs_path.read_text()
                log.info("crazy_check", path=sp, content=content.strip())

        log.info("test_crazy_mixed_deps", msg="PASSED")
        await store.close()
        await ctx.substitution_manager.close()


# ── main ───────────────────────────────────────────────────────────

_TESTS = {
    "test_simple_build": test_simple_build,
    "test_ca_simple": test_ca_simple,
    "test_ca_multi_output": test_ca_multi_output,
    "test_ca_depends_on_ca": test_ca_depends_on_ca,
    "test_deferred": test_deferred_non_ca_depends_on_ca,
    "test_ca_fixed": test_ca_fixed,
    "test_ca_text_hashed": test_ca_text_hashed,
    "test_dyn_hello": test_dyn_hello,
    "test_dyn_producing_drv": test_dyn_producing_drv,
    "test_dyn_wrapper": test_dyn_wrapper,
    "test_deep_dynamic": test_deep_dynamic,
    "test_crazy_mixed_deps": test_crazy_mixed_deps,
}


async def async_main() -> None:
    _setup_logging()

    requested = [a for a in sys.argv[1:] if not a.startswith("--")]
    tests = [fn for name, fn in _TESTS.items() if not requested or name in requested]

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
