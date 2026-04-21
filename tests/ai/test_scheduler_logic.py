import asyncio
import pytest
from pynixd.scheduler import Scheduler
from pynixd.operations.build_derivation import (
    BuildDerivationRequest,
    BuildDerivationResponse,
)
from pynixd.operations.base import BasicDerivation, BuildResult, BuildResultStatus
from pynixd.store_path import StorePath
from tests.ai.mock_store import MockStore


@pytest.mark.asyncio
async def test_scheduler_load_balancing():
    """Test that the scheduler correctly balances builds across stores."""

    # 1. Setup Virtual Fleet
    local_store = MockStore(
        "local", max_builds=1, feature_matrix={"x86_64-linux": set()}
    )
    remote1 = MockStore("remote1", max_builds=1, feature_matrix={"x86_64-linux": set()})

    scheduler = Scheduler(
        stores={"remote1": remote1},
        local_store=local_store,
        stream_paths_fn=MockStore.stream_paths_store_to_store,
    )

    # 2. Mock build response
    build_resp = BuildDerivationResponse(
        result=BuildResult(status=BuildResultStatus.BUILT)
    )
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

    # 5. Verify assignment (before completion/cleanup)
    # The build might complete instantly in the mock, but it should
    # still be in the queue until we call cleanup_completed.
    # However, since execute_build is a background task, we need
    # to yield control to let it set assigned_store_id.
    for _ in range(10):
        queued_build = scheduler.queue.by_id[build_id]
        if queued_build.assigned_store_id is not None:
            break
        await asyncio.sleep(0.01)

    assert queued_build.assigned_store_id == "remote1"

    # 6. Wait for completion
    resp = await future
    assert resp.result.status == BuildResultStatus.BUILT


@pytest.mark.asyncio
async def test_scheduler_skips_saturated_store():
    """Test that the scheduler waits if no slots are available."""

    local_store = MockStore(
        "local", max_builds=0, feature_matrix={"x86_64-linux": set()}
    )
    remote1 = MockStore("remote1", max_builds=0, feature_matrix={"x86_64-linux": set()})

    scheduler = Scheduler(
        stores={"remote1": remote1},
        local_store=local_store,
        stream_paths_fn=MockStore.stream_paths_store_to_store,
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
    assert queued_build.assigned_store_id is None
    assert queued_build.is_pending

    # Free a slot
    remote1.build_semaphore.release()

    await scheduler.schedule()

    # Wait for the build task to start and assign
    for _ in range(10):
        if queued_build.assigned_store_id is not None:
            break
        await asyncio.sleep(0.01)
    assert queued_build.assigned_store_id == "remote1"


@pytest.mark.asyncio
async def test_scheduler_proactive_transfer():
    """Test that the scheduler proactively pulls paths to an idle store."""

    # local MUST NOT have the path initially for us to test "pulling waiting paths"
    # Wait, proactive transfer is for "waiting_slot".
    # waiting_slot means all paths are in local_store.

    local_store = MockStore(
        "local", max_builds=1, feature_matrix={"x86_64-linux": set()}
    )
    remote_busy = MockStore(
        "busy", max_builds=0, feature_matrix={"x86_64-linux": set()}
    )
    remote_idle = MockStore(
        "idle", max_builds=1, feature_matrix={"x86_64-linux": set()}
    )

    drv_path = StorePath("/nix/store/00000000000000000000000000000001-test.drv")
    remote_busy.tracker.add_known_path(drv_path)
    local_store.tracker.add_known_path(drv_path)

    scheduler = Scheduler(
        stores={"busy": remote_busy, "idle": remote_idle},
        local_store=local_store,
        stream_paths_fn=MockStore.stream_paths_store_to_store,
    )

    # Mock build response for idle store
    idle_resp = BuildDerivationResponse(
        result=BuildResult(status=BuildResultStatus.BUILT)
    )
    remote_idle.responses[BuildDerivationRequest] = idle_resp

    request = BuildDerivationRequest(
        drv_path=drv_path, derivation=BasicDerivation(platform="x86_64-linux")
    )

    build_id, _future = await scheduler.build_derivation(
        request, client=None, required_paths={drv_path}, platform="x86_64-linux"
    )

    # Pass 1: busy is best (has paths) but saturated. idle has slot but needs paths.
    # Should assign to idle and start transfer.
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

    assert remote_idle.tracker.has_path(drv_path)


@pytest.mark.asyncio
async def test_scheduler_decomposition_and_ordering():
    """Test that BuildDecomposer correctly resolves a DAG and the Scheduler respects it."""
    from pynixd.derived_path import DerivedPath
    from pynixd.operations.base import BuildMode, BuildResult, BuildResultStatus
    from pynixd.operations.query_missing import (
        QueryMissingResponse,
        QueryMissingRequest,
    )
    from pynixd.operations.query_derivation_outputs_batch import (
        DerivationOutputsBatchResponse,
        QueryDerivationOutputsBatchRequest,
    )
    from pynixd.operations.query_valid_paths import (
        QueryValidPathsRequest,
        QueryValidPathsResponse,
    )
    from pynixd.drv_parser import ParsedDerivation, OutputInfo
    from pynixd.operations.build_derivation import (
        BuildDerivationRequest,
        BuildDerivationResponse,
    )

    # 1. Setup Fleet (remote1 can build everything)
    local_store = MockStore("local", feature_matrix={"x86_64-linux": set()})
    remote1 = MockStore(
        "remote1", max_builds=10, feature_matrix={"x86_64-linux": set()}
    )

    scheduler = Scheduler(
        stores={"remote1": remote1},
        local_store=local_store,
        stream_paths_fn=MockStore.stream_paths_store_to_store,
    )

    leaf_path = StorePath("/nix/store/00000000000000000000000000000001-leaf.drv")
    root_path = StorePath("/nix/store/00000000000000000000000000000002-root.drv")
    local_store.tracker.add_known_paths({leaf_path, root_path})

    # 2. Mock Responses for Decomposer
    local_store.responses[QueryMissingRequest] = QueryMissingResponse(
        will_build={str(leaf_path), str(root_path)}
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

    def mock_read_drv(store_path, drv_path):
        if str(drv_path) == str(leaf_path):
            return leaf_drv
        if str(drv_path) == str(root_path):
            return root_drv
        raise FileNotFoundError(drv_path)

    scheduler.decomposer.read_drv_fn = mock_read_drv

    # 3. Mock build response for remote1
    remote1.responses[BuildDerivationRequest] = BuildDerivationResponse(
        result=BuildResult(status=BuildResultStatus.BUILT)
    )
    # Block BOTH builds initially
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

    # Find builds
    root_b = [
        b for b in scheduler.queue.queue if str(b.request.drv_path) == str(root_path)
    ][0]
    leaf_b = [
        b for b in scheduler.queue.queue if str(b.request.drv_path) == str(leaf_path)
    ][0]

    # 5. Run first scheduling pass
    await scheduler.schedule()
    await asyncio.sleep(0.05)

    # Verify: Only leaf is scheduled and building
    assert leaf_b.assigned_store_id == "remote1"
    assert leaf_b.is_building
    assert root_b.assigned_store_id is None
    assert root_b.is_pending

    # 6. Complete leaf build
    leaf_done.set()
    await asyncio.sleep(0.05)
    assert leaf_b.is_done

    # 7. Run second pass: root should now be schedulable
    await scheduler.schedule()
    await asyncio.sleep(0.05)
    assert root_b.assigned_store_id == "remote1"
    assert root_b.is_building

    # 8. Complete root build
    root_done.set()
    await asyncio.sleep(0.05)
    assert root_b.is_done

    # 9. Final result
    results = await build_task
    assert len(results.results) == 1
    assert results.results[0].result.status == BuildResultStatus.BUILT


@pytest.mark.asyncio
async def test_scheduler_cpu_utilization():
    """Test that the scheduler avoids stores with high CPU utilization."""
    from pynixd.operations.build_derivation import (
        BuildDerivationRequest,
        BuildDerivationResponse,
    )

    local_store = MockStore(
        "local", max_builds=1, feature_matrix={"x86_64-linux": set()}
    )
    remote_hot = MockStore(
        "hot",
        max_builds=1,
        feature_matrix={"x86_64-linux": set()},
        cpu_utilization=100.0,
    )
    remote_cold = MockStore(
        "cold",
        max_builds=1,
        feature_matrix={"x86_64-linux": set()},
        cpu_utilization=10.0,
    )

    scheduler = Scheduler(
        stores={"hot": remote_hot, "cold": remote_cold},
        local_store=local_store,
        stream_paths_fn=MockStore.stream_paths_store_to_store,
    )

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
