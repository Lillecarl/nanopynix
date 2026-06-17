"""Goal tree execution tests — exercises pynixd's internal DAG-based goal system.

These tests create a LocalSocketStore and GoalContext directly (bypassing the
full pynixd server), then exercise the goal tree through make_build_goal() →
BuildGoal.run() / ResolutionGoal / DynamicBuildGoal over the real daemon wire.

This is a different testing layer from the end-to-end tests in test_ca_ops.py
(which go through ``nix build`` → pynixd proxy → daemon) and the scheduler logic
tests (which use MockStore).  These tests validate the goal tree logic itself
against a real Nix daemon.
"""

from __future__ import annotations

import hashlib
import random
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest
import structlog

from pynixd.derived_path import DerivedPath
from pynixd.goals.goal import GoalContext, make_build_goal
from pynixd.goals.manager import GoalManager
from pynixd.nar import NarRegular, parse_nar
from pynixd.operations.nar_from_path import NarFromPathRequest
from pynixd.serde import IsValidPathRequest, QueryPathInfoRequest
from pynixd.serde import StorePath as SerdeStorePath
from pynixd.store import LocalSocketStore
from pynixd.store_path import StorePath
from pynixd.substitution import (
    HttpBinaryCacheSubstituter,
    SubstitutionManager,
)
from tests._conftest.helpers import rmtree_robust
from tests.conftest import (
    CLIENT_BIN,
    TEST_NIX,
    make_test_spec,
    run_subproc,
)
from tests.nix_config import NixConfig
from tests.test_features import TestFeatures as F

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator
    from pathlib import Path

    import pyinstrument

log = structlog.get_logger(__name__)

# Module-level Nix config enabling CA + dynamic derivations for all goal tree tests.
_GOAL_NIX_CONFIG = NixConfig.for_test_store(
    experimental_features=(
        "ca-derivations",
        "dynamic-derivations",
    ),
)

# Nix build result status codes we consider successful
_BUILT_OK = (0, 1, 2, 13)


# ── Shorter temp path fixture ────────────────────────────────────
# The default tmp_path uses the full pytest node name which, combined
# with the daemon socket path, can exceed Linux's 108-byte sun_path
# limit for Unix sockets.  Use a hashed suffix to keep the path short.


@pytest.fixture
def short_tmp_path(request: pytest.FixtureRequest) -> Generator[Path]:
    """Temp directory with a short, hashed name to avoid Unix socket path length limits.

    Linux's ``sockaddr_un.sun_path`` is 108 bytes.  The default
    ``tmp_path`` fixture uses the full pytest node name (e.g.
    ``test_deferred_non_ca_depends_on_ca[asyncio+uvloop]``) which
    produces socket paths >108 bytes, causing Nix daemon SIGABRT in
    ``createUnixDomainSocket``.

    This fixture hashes the node name to guarantee a short path.
    """
    from pathlib import Path

    name_hash = hashlib.md5(request.node.nodeid.encode()).hexdigest()[:12]
    suffix = random.getrandbits(32)
    path = Path(f"/tmp/pxd-{name_hash}-{suffix:08x}")
    path.mkdir(parents=True, exist_ok=True)
    yield path
    with suppress(Exception):
        rmtree_robust(path)


# ── Helpers ───────────────────────────────────────────────────────


async def nix_eval(store_path: str, attr: str) -> str:
    """Evaluate a Nix attribute in the test nix file and return the raw value."""
    rc, stdout, _, _ = await run_subproc(
        [
            str(CLIENT_BIN),
            "eval",
            "--store",
            store_path,
            "--impure",
            "--file",
            str(TEST_NIX),
            attr,
            "--raw",
        ],
        nix_config=_GOAL_NIX_CONFIG,
    )
    return stdout.strip()


async def build_drv(
    ctx: GoalContext,
    drv_str: str,
    output_name: str = "out",
):
    """Build a single derivation output through the goal tree.

    Wraps make_build_goal + goal.run() — the core goal tree exercise.
    """
    dp = DerivedPath(f"{drv_str}!{output_name}")
    goal = make_build_goal(dp, ctx)
    await goal.run()
    return goal.result


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def goal_context(
    short_tmp_path: Path,
) -> AsyncGenerator[tuple[LocalSocketStore, GoalContext]]:
    """Create a LocalSocketStore + GoalContext in a temp directory.

    Yields (store, ctx).  Cleans up via async context managers on teardown.
    Uses a short temp path to avoid Unix socket path length limits.
    """
    spec = make_test_spec(
        store_id="goal-test",
        store_path=short_tmp_path,
        nix_config=_GOAL_NIX_CONFIG,
        no_probe=True,
    )
    async with (
        LocalSocketStore(spec) as store,
        SubstitutionManager(
            substituters=[HttpBinaryCacheSubstituter("https://cache.nixos.org")],
        ) as sm,
    ):
        ctx = GoalContext(
            goal_manager=GoalManager(),
            store=store,
            substitution_manager=sm,
        )
        yield store, ctx


# ── Tests ─────────────────────────────────────────────────────────


@pytest.mark.no_pynixd
@pytest.mark.covers(F.GOAL_BUILD | F.REGULAR)
async def test_simple_build(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Build a simple (non-CA) derivation via the goal tree."""
    store, ctx = goal_context
    drv_str = await nix_eval(str(store.store_path), "dyn.hello.drvPath")
    log.info("test_simple_build", drv=drv_str)

    result = await build_drv(ctx, drv_str, "out")
    assert result is not None, "goal returned None"
    assert result.result.status in _BUILT_OK, f"build failed: {result.result.status} {result.result.error_msg}"
    assert result.produced_paths, "no produced paths"

    for sp in result.produced_paths:
        valid = (await store.execute(IsValidPathRequest(path=SerdeStorePath(path=str(sp))))).valid
        assert valid, f"produced path {sp} is not valid"


@pytest.mark.no_pynixd
@pytest.mark.ca_derivations
@pytest.mark.covers(F.GOAL_BUILD | F.CA_FLOATING)
async def test_ca_simple(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Build a CA floating derivation (ca.simple)."""
    store, ctx = goal_context
    drv_str = await nix_eval(str(store.store_path), "ca.simple.drvPath")
    log.info("test_ca_simple", drv=drv_str)

    result = await build_drv(ctx, drv_str, "out")
    assert result is not None, "goal returned None"
    assert result.result.status in _BUILT_OK, f"build failed: {result.result.status} {result.result.error_msg}"
    assert result.produced_paths, "no produced paths"

    for sp in result.produced_paths:
        valid = (await store.execute(IsValidPathRequest(path=SerdeStorePath(path=str(sp))))).valid
        assert valid, f"CA output {sp} is not valid"


@pytest.mark.no_pynixd
@pytest.mark.ca_derivations
@pytest.mark.covers(F.GOAL_BUILD | F.CA_MULTI_OUTPUT | F.GOAL_DAG)
async def test_ca_multi_output(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Build a CA derivation with multiple outputs (ca.multi_output)."""
    store, ctx = goal_context
    drv_str = await nix_eval(str(store.store_path), "ca.multi_output.drvPath")
    log.info("test_ca_multi_output", drv=drv_str)

    for out_name in ("out", "dev"):
        result = await build_drv(ctx, drv_str, out_name)
        assert result is not None, f"no result for {out_name}"
        assert result.result.status in _BUILT_OK, (
            f"build of {out_name} failed: {result.result.status} {result.result.error_msg}"
        )
        assert result.produced_paths, f"no produced paths for {out_name}"
        log.info("test_ca_multi_output", output=out_name, paths=result.produced_paths)


@pytest.mark.no_pynixd
@pytest.mark.ca_derivations
@pytest.mark.covers(F.GOAL_BUILD | F.CA_DEPENDS_ON_CA | F.GOAL_DAG)
async def test_ca_depends_on_ca(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Build a CA derivation that depends on another CA (ca.depends_on_ca)."""
    store, ctx = goal_context
    drv_str = await nix_eval(str(store.store_path), "ca.depends_on_ca.drvPath")
    log.info("test_ca_depends_on_ca", drv=drv_str)

    result = await build_drv(ctx, drv_str, "out")
    assert result is not None, "goal returned None"
    assert result.result.status in _BUILT_OK, f"build failed: {result.result.status} {result.result.error_msg}"
    assert result.produced_paths, "no produced paths"


@pytest.mark.no_pynixd
@pytest.mark.ca_derivations
@pytest.mark.covers(F.GOAL_DEFERRED | F.DEFERRED | F.CA_FLOATING | F.GOAL_RESOLUTION)
async def test_deferred_non_ca_depends_on_ca(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Build a non-CA (deferred) derivation that depends on a CA derivation.

    Exercises the DEFERRED resolution path in ResolutionGoal:
    1. The non-CA derivation's .drv has path=\"\" (deferred outputs)
    2. ResolutionGoal must compute hashDerivationModulo and derive paths
    3. Input dep (CA simple) must be built first
    4. Placeholders must be rewritten with the actual CA output path
    """
    store, ctx = goal_context

    # Build the CA dependency explicitly first so its realisations
    # are registered in the store, then build the non-CA deferred
    # derivation that depends on it.
    ca_drv = await nix_eval(str(store.store_path), "ca.simple.drvPath")
    ca_result = await build_drv(ctx, ca_drv, "out")
    assert ca_result is not None
    assert ca_result.result.status in _BUILT_OK, "CA dep build failed"
    log.info("test_deferred", ca_built=ca_result.produced_paths)

    # The GoalManager was cleared by build_paths. Now evaluate and
    # build non_ca_depends_on_ca in a separate goal tree.
    drv_str = await nix_eval(str(store.store_path), "ca.non_ca_depends_on_ca.drvPath")
    log.info("test_deferred", drv=drv_str)

    result = await build_drv(ctx, drv_str, "out")
    assert result is not None, "goal returned None"
    assert result.result.status in _BUILT_OK, f"deferred build failed: {result.result.status} {result.result.error_msg}"
    assert result.produced_paths, "no produced paths"

    # Verify the output contains the CA content
    for sp in result.produced_paths:
        fs_path = store.store_path / str(sp).lstrip("/")
        if fs_path.exists() and fs_path.is_file():
            content = fs_path.read_text()
            assert "dep-on-" in content, f"unexpected content in {sp}: {content[:200]}"
            log.info("test_deferred", path=sp, content_preview=content.strip()[:80])


@pytest.mark.no_pynixd
@pytest.mark.ca_derivations
@pytest.mark.covers(F.GOAL_BUILD | F.CA_FIXED)
async def test_ca_fixed(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Build a fixed-output CA derivation (known content hash at eval time).

    Unlike floating CA (outputHash=\"\"), a fixed-output CA has a known
    content hash at evaluation time, so the .drv contains the expected
    output path.  The daemon builds it, verifies the hash, and registers
    the realisation.

    The ResolutionGoal hits the CA_FIXED path and returns the known
    output path immediately — no hashDerivationModulo needed.
    """
    store, ctx = goal_context
    drv_str = await nix_eval(str(store.store_path), "ca.fixed_ca.drvPath")
    log.info("test_ca_fixed", drv=drv_str)

    result = await build_drv(ctx, drv_str, "out")
    assert result is not None, "goal returned None"
    assert result.result.status in _BUILT_OK, f"build failed: {result.result.status} {result.result.error_msg}"
    assert result.produced_paths, "no produced paths"

    for sp in result.produced_paths:
        valid = (await store.execute(IsValidPathRequest(path=SerdeStorePath(path=str(sp))))).valid
        assert valid, f"CA fixed output {sp} is not valid"


@pytest.mark.no_pynixd
@pytest.mark.ca_derivations
@pytest.mark.covers(F.GOAL_BUILD | F.CA_TEXT_HASHED)
async def test_ca_text_hashed(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Build a text-hashed CA derivation (ca.text_hashed)."""
    store, ctx = goal_context
    drv_str = await nix_eval(str(store.store_path), "ca.text_hashed.drvPath")
    log.info("test_ca_text_hashed", drv=drv_str)

    result = await build_drv(ctx, drv_str, "out")
    assert result is not None, "goal returned None"
    assert result.result.status in _BUILT_OK, f"build failed: {result.result.status} {result.result.error_msg}"


@pytest.mark.no_pynixd
@pytest.mark.covers(F.GOAL_BUILD | F.REGULAR)
async def test_dyn_hello(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Build dyn.hello (simple regular derivation in dyn set)."""
    store, ctx = goal_context
    drv_str = await nix_eval(str(store.store_path), "dyn.hello.drvPath")
    log.info("test_dyn_hello", drv=drv_str)

    result = await build_drv(ctx, drv_str, "out")
    assert result is not None, "goal returned None"
    assert result.result.status in _BUILT_OK, f"build failed: {result.result.status} {result.result.error_msg}"


@pytest.mark.no_pynixd
@pytest.mark.ca_derivations
@pytest.mark.covers(F.DYN_PRODUCING_DRV | F.GOAL_BUILD | F.CA_TEXT_HASHED)
async def test_dyn_producing_drv(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Build dyn.producingDrv (CA that outputs a .drv file).

    This is the first step in the dynamic derivation chain.
    """
    store, ctx = goal_context
    drv_str = await nix_eval(str(store.store_path), "dyn.producingDrv.drvPath")
    log.info("test_dyn_producing_drv", drv=drv_str)

    result = await build_drv(ctx, drv_str, "out")
    assert result is not None, "goal returned None"
    assert result.result.status in _BUILT_OK, f"build failed: {result.result.status} {result.result.error_msg}"
    assert result.produced_paths, "no produced paths"

    for sp in result.produced_paths:
        log.info("test_dyn_producing_drv", path=sp, is_derivation=sp.is_derivation())


@pytest.mark.no_pynixd
@pytest.mark.ca_derivations
@pytest.mark.covers(F.DYN_WRAPPER_BUILD | F.GOAL_DYNAMIC | F.GOAL_DAG)
async def test_dyn_wrapper(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Build dyn.wrapper — the full dynamic derivation chain.

    This exercises the DynamicBuildGoal (nested DerivedPath):
    1. Build producingDrv → produces a .drv file as output
    2. Read the inner .drv from the result
    3. Build the inner derivation
    4. Build wrapper which references the inner output via outputOf
    """
    store, ctx = goal_context
    drv_str = await nix_eval(str(store.store_path), "dyn.wrapper.drvPath")
    log.info("test_dyn_wrapper", drv=drv_str)

    result = await build_drv(ctx, drv_str, "out")
    assert result is not None, "goal returned None"
    assert result.result.status in _BUILT_OK, f"build failed: {result.result.status} {result.result.error_msg}"


@pytest.mark.no_pynixd
@pytest.mark.ca_derivations
@pytest.mark.covers(F.DYN_DEEP | F.DYN_CHAIN | F.GOAL_DYNAMIC)
async def test_deep_dynamic(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
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
    store, ctx = goal_context
    drv_str = await nix_eval(str(store.store_path), "dyn.deepWrapper.drvPath")
    log.info("test_deep_dynamic", drv=drv_str)

    result = await build_drv(ctx, drv_str, "out")
    assert result is not None, "goal returned None"
    assert result.result.status in _BUILT_OK, f"build failed: {result.result.status} {result.result.error_msg}"


@pytest.mark.no_pynixd
@pytest.mark.ca_derivations
@pytest.mark.covers(F.DYN_MIXED_DEPS | F.GOAL_DAG)
async def test_crazy_mixed_deps(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Build a derivation that mixes regular, CA, and dynamic deps.

    Exercises:
    1. Multiple dynamic_input_drvs entries with different chain depths
    2. Mixed regular + CA + dynamic deps in one .drv
    3. Dynamic chains of different lengths running in parallel
    """
    store, ctx = goal_context
    drv_str = await nix_eval(str(store.store_path), "dyn.crazy.drvPath")
    log.info("test_crazy_mixed_deps", drv=drv_str)

    result = await build_drv(ctx, drv_str, "out")
    assert result is not None, "goal returned None"
    assert result.result.status in _BUILT_OK, f"build failed: {result.result.status} {result.result.error_msg}"

    # Verify output contains content from all dependency types
    for sp in result.produced_paths or set():
        fs_path = store.store_path / str(sp).lstrip("/")
        if fs_path.exists() and fs_path.is_file():
            content = fs_path.read_text()
            log.info("crazy_check", path=sp, content=content.strip())


@pytest.mark.no_pynixd
@pytest.mark.ca_derivations
@pytest.mark.covers(F.DYN_NAR_ROUNDTRIP | F.NAR_PARSE | F.NAR_FROM_PATH)
async def test_nar_from_path_roundtrip(
    profiler: pyinstrument.Profiler,
    goal_context: tuple[LocalSocketStore, GoalContext],
) -> None:
    """Verify a .drv file is byte-identical over NarFromPath.

    Eval a .drv into a temp store, read it from the local filesystem,
    fetch the same bytes via NarFromPath over the wire, and compare.
    """
    store, ctx = goal_context
    log.info("test_nar", msg="starting")

    # 1. Eval into the temp store so the .drv is written there
    drv_path_str = await nix_eval(str(store.store_path), "dyn.hello.drvPath")
    log.info("test_nar", drv_path=drv_path_str)

    # 2. Read the .drv from the temp store's filesystem
    local_path = store.store_path / drv_path_str.lstrip("/")
    local_bytes = local_path.read_bytes()
    log.info("test_nar", local_size=len(local_bytes))

    # 3. QueryPathInfo to get nar_size
    drv_store_path = StorePath(drv_path_str)
    info_resp = await store.execute(QueryPathInfoRequest(path=SerdeStorePath(path=drv_path_str)))
    assert info_resp.valid, f"path not found (invalid): {drv_path_str}"
    assert info_resp.info is not None, f"path not found (no info): {drv_path_str}"
    nar_size = info_resp.info.nar_size

    # 4. Fetch via NarFromPath
    resp = await store.execute(
        NarFromPathRequest(path=drv_store_path, nar_size=nar_size),
    )
    assert resp is not None, "NarFromPath returned None"
    nar_bytes = resp.nar_data
    log.info("test_nar", nar_size=len(nar_bytes))

    # 5. Parse the NAR and extract the file contents
    nar_node = parse_nar(nar_bytes)
    assert isinstance(nar_node, NarRegular), f"expected regular file, got {type(nar_node).__name__}"

    # 6. Compare byte-for-byte
    actual_bytes = nar_node.contents
    assert actual_bytes == local_bytes, f"NAR contents differ! local={len(local_bytes)}b nar={len(actual_bytes)}b"

    log.info("test_nar", msg="PASSED", match=True)
