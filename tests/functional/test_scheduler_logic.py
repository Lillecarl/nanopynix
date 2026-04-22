import asyncio
import pytest
from pynixd.scheduler import Scheduler
from pynixd.context import PynixdContext
from pynixd.config import PynixdSettings
from pynixd.path_tracker import PathTracker
from pynixd.operations.build_derivation import (
    BuildDerivationRequest,
    BuildDerivationResponse,
)
from pynixd.derived_path import DerivedPath
from pynixd.drv_parser import ParsedDerivation, OutputInfo
from pynixd.operations.base import (
    BasicDerivation,
    BuildMode,
    BuildResult,
    BuildResultStatus,
)
from pynixd.operations.query_missing import (
    QueryMissingRequest,
    QueryMissingResponse,
)
from pynixd.operations.query_derivation_outputs_batch import (
    QueryDerivationOutputsBatchRequest,
    DerivationOutputsBatchResponse,
)
from pynixd.operations.query_valid_paths import (
    QueryValidPathsRequest,
    QueryValidPathsResponse,
)
from pynixd.store_path import StorePath
from tests.functional.mock_store import MockStore

"""
Deterministic Scheduler Logic Tests

These tests use the `MockStore` to verify the pynixd Scheduler's 
routing, load-balancing, and DAG decomposition logic without 
requiring a real Nix daemon or filesystem state.

By virtualizing all I/O and controlling build completion timing, 
we can assert on complex behaviors (like proactive transfers or 
PSI-aware routing) with zero flakiness and high speed.
"""


@pytest.mark.asyncio
async def test_scheduler_load_balancing():
    """Verify that the scheduler correctly assigns builds to idle remote stores.

    Setup:
    - 1 Local store (1 slot)
    - 1 Remote store (1 slot)

    Success Condition:
    - Build is enqueued and assigned to 'remote1'.
    - Manual schedule pass triggers execution.
    """

    # 1. Setup Virtual Fleet
    local_store = MockStore("local", feature_matrix={"x86_64-linux": set()})
    remote1 = MockStore("remote1", feature_matrix={"x86_64-linux": set()})

    ctx = PynixdContext(
        settings=PynixdSettings(),
        local_store=local_store,
        stores={"remote1": remote1},
        path_tracker=PathTracker(db=None),
    )
    scheduler = Scheduler(ctx)

    # 2. Mock build response for all stores
    build_resp = BuildDerivationResponse(
        result=BuildResult(status=BuildResultStatus.BUILT)
    )
    local_store.responses[BuildDerivationRequest] = build_resp
    remote1.responses[BuildDerivationRequest] = build_resp

    # 3. Enqueue a build
    drv_path = StorePath("/nix/store/00000000000000000000000000000001-test.drv")
    local_store.tracker.add_known_path(drv_path)
    request = BuildDerivationRequest(
        drv_path=drv_path, derivation=BasicDerivation(platform="x86_64-linux")
    )

    build_id, future = await scheduler.build_derivation(
        request, client=None, required_paths={drv_path}, platform="x86_64-linux"
    )

    # 4. Trigger scheduler manually
    await scheduler.schedule()

    # 5. Verify assignment (polling to account for background task startup)
    queued_build = scheduler.queue.by_id[build_id]
    for _ in range(10):
        if queued_build.assigned_store_id is not None:
            break
        await asyncio.sleep(0.01)

    assert queued_build.assigned_store_id == "remote1"

    # 6. Wait for completion
    resp = await future
    assert resp.result.status == BuildResultStatus.BUILT


@pytest.mark.asyncio
async def test_scheduler_skips_saturated_store():
    """Verify that the scheduler waits for available slots instead of over-subscribing.

    Setup:
    - Remote store with 0 available slots.

    Success Condition:
    - Build remains pending after first pass.
    - Build is assigned only after a slot is manually released.
    """

    local_store = MockStore("local", feature_matrix={"x86_64-linux": set()})
    remote1 = MockStore("remote1", feature_matrix={"x86_64-linux": set()})

    # Simulate saturation by manually incrementing active connections
    # A concurrency penalty of 50.0 per connection will push the score below 0.0
    # (Assuming base score is 100 from CPU idle)
    local_store.pool.active_connections = 3
    remote1.pool.active_connections = 3

    ctx = PynixdContext(
        settings=PynixdSettings(),
        local_store=local_store,
        stores={"remote1": remote1},
        path_tracker=PathTracker(db=None),
    )
    scheduler = Scheduler(ctx)

    # Ensure all stores have a build response before we start
    build_resp = BuildDerivationResponse(
        result=BuildResult(status=BuildResultStatus.BUILT)
    )
    local_store.responses[BuildDerivationRequest] = build_resp
    remote1.responses[BuildDerivationRequest] = build_resp

    drv_path = StorePath("/nix/store/00000000000000000000000000000001-test.drv")
    local_store.tracker.add_known_path(drv_path)
    request = BuildDerivationRequest(
        drv_path=drv_path, derivation=BasicDerivation(platform="x86_64-linux")
    )

    build_id, _future = await scheduler.build_derivation(
        request, client=None, required_paths={drv_path}, platform="x86_64-linux"
    )

    # Pass 1: No slots available anywhere (scores < 0)
    await scheduler.schedule()

    queued_build = scheduler.queue.by_id[build_id]
    assert queued_build.assigned_store_id is None
    assert queued_build.is_pending

    # 2. Free a slot on remote1
    remote1.pool.active_connections = 0

    # Pass 2: Now it should be assigned
    await scheduler.schedule()

    # Wait for the build task to start and assign
    for _ in range(10):
        if queued_build.assigned_store_id is not None:
            break
        await asyncio.sleep(0.01)
    assert queued_build.assigned_store_id == "remote1"


@pytest.mark.asyncio
async def test_scheduler_proactive_transfer():
    """Verify that the scheduler proactively pulls paths to an idle store.

    Scenario:
    - Store 'busy' has the inputs but no slots.
    - Store 'idle' has slots but no inputs.

    Success Condition:
    - Scheduler assigns build to 'idle'.
    - Scheduler triggers `transfer_inputs` (simulated by stream_paths_fn).
    - Inputs are present on 'idle' before build execution starts.
    """

    local_store = MockStore("local", feature_matrix={"x86_64-linux": set()})
    remote_busy = MockStore("busy", feature_matrix={"x86_64-linux": set()})
    remote_idle = MockStore("idle", feature_matrix={"x86_64-linux": set()})

    # Saturate 'busy' store
    # With locality_weight=500 and cpu_idle_weight=100, we need > (500+100)/50 = 12 conns
    remote_busy.pool.active_connections = 13

    drv_path = StorePath("/nix/store/00000000000000000000000000000001-test.drv")
    remote_busy.tracker.add_known_path(drv_path)
    local_store.tracker.add_known_path(drv_path)

    ctx = PynixdContext(
        settings=PynixdSettings(),
        local_store=local_store,
        stores={"busy": remote_busy, "idle": remote_idle},
        path_tracker=PathTracker(db=None),
    )
    scheduler = Scheduler(ctx)

    # Mock build response for all stores
    build_resp = BuildDerivationResponse(
        result=BuildResult(status=BuildResultStatus.BUILT)
    )
    local_store.responses[BuildDerivationRequest] = build_resp
    remote_busy.responses[BuildDerivationRequest] = build_resp
    remote_idle.responses[BuildDerivationRequest] = build_resp

    request = BuildDerivationRequest(
        drv_path=drv_path, derivation=BasicDerivation(platform="x86_64-linux")
    )

    build_id, _future = await scheduler.build_derivation(
        request, client=None, required_paths={drv_path}, platform="x86_64-linux"
    )

    # Pass 1: busy is best (has paths) but saturated. idle has slot but needs paths.
    await scheduler.schedule()

    queued_build = scheduler.queue.by_id[build_id]

    # Wait for assignment
    for _ in range(10):
        if queued_build.assigned_store_id is not None:
            break
        await asyncio.sleep(0.01)

    # It should be assigned to idle because busy has 0 slots
    assert queued_build.assigned_store_id == "idle"

    # Yield control to let execute_build (and stream_paths) finish
    await asyncio.sleep(0.05)

    # Verify path was moved to the idle store
    assert remote_idle.tracker.has_path(drv_path)


@pytest.mark.asyncio
async def test_scheduler_decomposition_and_ordering():
    """Verify that BuildDecomposer correctly resolves a DAG and the Scheduler respects it.

    Scenario:
    - Enqueue a 'root' derivation that depends on a 'leaf' derivation.

    Success Condition:
    - Queue contains both builds.
    - Leaf is scheduled and completed first.
    - Root is only scheduled AFTER leaf completes.
    """
    local_store = MockStore("local", feature_matrix={"x86_64-linux": set()})
    remote1 = MockStore("remote1", feature_matrix={"x86_64-linux": set()})

    ctx = PynixdContext(
        settings=PynixdSettings(),
        local_store=local_store,
        stores={"remote1": remote1},
        path_tracker=PathTracker(db=None),
    )
    scheduler = Scheduler(ctx)

    leaf_path = StorePath("/nix/store/00000000000000000000000000000001-leaf.drv")
    root_path = StorePath("/nix/store/00000000000000000000000000000002-root.drv")
    local_store.tracker.add_known_paths({leaf_path, root_path})

    # 2. Mock Responses for the BuildDecomposer pipeline
    local_store.responses[QueryMissingRequest] = QueryMissingResponse(
        will_build={leaf_path, root_path}
    )
    local_store.responses[QueryValidPathsRequest] = QueryValidPathsResponse(paths=set())
    local_store.responses[QueryDerivationOutputsBatchRequest] = (
        DerivationOutputsBatchResponse(
            outputs={leaf_path: {"out": StorePath("/nix/store/leaf-out")}}
        )
    )

    leaf_drv = ParsedDerivation(
        outputs=[
            OutputInfo(
                name="out", path="/nix/store/leaf-out", hash_algo="", hash_value=""
            )
        ],
        input_drvs={},
        input_srcs=set(),
        platform="x86_64-linux",
    )
    root_drv = ParsedDerivation(
        outputs=[
            OutputInfo(
                name="out", path="/nix/store/root-out", hash_algo="", hash_value=""
            )
        ],
        input_drvs={leaf_path: ["out"]},
        input_srcs=set(),
        platform="x86_64-linux",
    )

    # Inject deterministic derivation reader
    def mock_read_drv(_store_path, drv_path):
        if str(drv_path) == str(leaf_path):
            return leaf_drv
        if str(drv_path) == str(root_path):
            return root_drv
        raise FileNotFoundError(drv_path)

    scheduler.decomposer.read_drv_fn = mock_read_drv

    # 3. Setup build responders
    build_resp = BuildDerivationResponse(
        result=BuildResult(status=BuildResultStatus.BUILT)
    )
    local_store.responses[BuildDerivationRequest] = build_resp
    remote1.responses[BuildDerivationRequest] = build_resp

    # Block BOTH builds initially so we can check queue state before completion
    leaf_done = remote1.block_build(leaf_path)
    root_done = remote1.block_build(root_path)

    # 4. Start decomposition in background
    dp = DerivedPath(str(root_path))
    build_task = asyncio.create_task(
        scheduler.build_derived_paths({dp}, BuildMode.NORMAL)
    )

    # Wait for decomposition to finish and populate queue
    await asyncio.sleep(0.05)
    assert len(scheduler.queue.queue) == 2

    # Find builds in queue
    root_b = [
        b for b in scheduler.queue.queue if str(b.request.drv_path) == str(root_path)
    ][0]
    leaf_b = [
        b for b in scheduler.queue.queue if str(b.request.drv_path) == str(leaf_path)
    ][0]

    # 5. Pass 1: Scheduling
    await scheduler.schedule()
    await asyncio.sleep(0.05)

    # Verify: Only leaf is scheduled (root blocked by depends_on)
    assert leaf_b.assigned_store_id == "remote1"
    assert leaf_b.is_building
    assert root_b.assigned_store_id is None
    assert root_b.is_pending

    # 6. Complete leaf build
    leaf_done.set()
    await asyncio.sleep(0.05)
    assert leaf_b.is_done

    # 7. Pass 2: Root should now be schedulable
    await scheduler.schedule()
    await asyncio.sleep(0.05)
    assert root_b.assigned_store_id == "remote1"
    assert root_b.is_building

    # 8. Complete root build
    root_done.set()
    await asyncio.sleep(0.05)
    assert root_b.is_done

    # 9. Final result verification
    results = await build_task
    assert len(results.results) == 1
    assert results.results[0].result.status == BuildResultStatus.BUILT


@pytest.mark.asyncio
async def test_scheduler_cpu_utilization():
    """Verify that the scheduler avoids stores with high CPU utilization (PSI aware).

    Setup:
    - Store 'hot': 100% CPU utilization
    - Store 'cold': 10% CPU utilization

    Success Condition:
    - Build is enqueued.
    - Scheduler assigns build to 'cold' store, bypassing 'hot'.
    """
    local_store = MockStore("local", feature_matrix={"x86_64-linux": set()})
    remote_hot = MockStore(
        "hot",
        feature_matrix={"x86_64-linux": set()},
        cpu_utilization=100.0,
    )
    remote_cold = MockStore(
        "cold",
        feature_matrix={"x86_64-linux": set()},
        cpu_utilization=10.0,
    )

    ctx = PynixdContext(
        settings=PynixdSettings(),
        local_store=local_store,
        stores={"hot": remote_hot, "cold": remote_cold},
        path_tracker=PathTracker(db=None),
    )
    scheduler = Scheduler(ctx)

    # remote_cold will handle the build
    remote_cold.responses[BuildDerivationRequest] = BuildDerivationResponse(
        result=BuildResult(status=BuildResultStatus.BUILT)
    )

    drv_path = StorePath("/nix/store/00000000000000000000000000000001-test.drv")
    local_store.tracker.add_known_path(drv_path)
    request = BuildDerivationRequest(
        drv_path=drv_path, derivation=BasicDerivation(platform="x86_64-linux")
    )

    build_id, _future = await scheduler.build_derivation(
        request, client=None, required_paths={drv_path}, platform="x86_64-linux"
    )

    await scheduler.schedule()

    queued_build = scheduler.queue.by_id[build_id]

    # Wait for assignment
    for _ in range(10):
        if queued_build.assigned_store_id is not None:
            break
        await asyncio.sleep(0.01)

    assert queued_build.assigned_store_id == "cold"


@pytest.mark.asyncio
async def test_scheduler_feature_matching():
    """Verify that the scheduler respects Store system/feature matrix requirements.

    Scenario:
    - Enqueue a build requiring 'kvm' and 'big-parallel'.
    - Store 'plain': Supports x86_64-linux but no extra features.
    - Store 'full': Supports x86_64-linux with 'kvm' and 'big-parallel'.

    Success Condition:
    - Scheduler assigns build to 'full' store.
    - 'plain' is correctly ignored due to missing features.
    """
    local_store = MockStore("local", feature_matrix={})

    # plain supports the system but not the features
    remote_plain = MockStore(
        "plain", feature_matrix={"x86_64-linux": {"ca-derivations"}}
    )

    # full supports both
    remote_full = MockStore(
        "full",
        feature_matrix={"x86_64-linux": {"ca-derivations", "kvm", "big-parallel"}},
    )

    ctx = PynixdContext(
        settings=PynixdSettings(),
        local_store=local_store,
        stores={"plain": remote_plain, "full": remote_full},
        path_tracker=PathTracker(db=None),
    )
    scheduler = Scheduler(ctx)

    remote_full.responses[BuildDerivationRequest] = BuildDerivationResponse(
        result=BuildResult(status=BuildResultStatus.BUILT)
    )

    drv_path = StorePath("/nix/store/00000000000000000000000000000001-test.drv")
    local_store.tracker.add_known_path(drv_path)

    # Create request with required features
    derivation = BasicDerivation(platform="x86_64-linux")
    derivation.env["requiredSystemFeatures"] = "kvm big-parallel"

    request = BuildDerivationRequest(drv_path=drv_path, derivation=derivation)

    build_id, _future = await scheduler.build_derivation(
        request, client=None, required_paths={drv_path}, platform="x86_64-linux"
    )

    await scheduler.schedule()

    queued_build = scheduler.queue.by_id[build_id]

    # Wait for assignment
    for _ in range(10):
        if queued_build.assigned_store_id is not None:
            break
        await asyncio.sleep(0.01)

    assert queued_build.assigned_store_id == "full"
