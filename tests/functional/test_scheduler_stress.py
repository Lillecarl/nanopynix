import asyncio

import structlog

from pynixd.config import PynixdSettings
from pynixd.context import PynixdContext
from pynixd.operations.base import (
    BasicDerivation,
    BuildResult,
    BuildResultStatus,
    UnkeyedValidPathInfo,
)
from pynixd.operations.build_derivation import (
    BuildDerivationRequest,
    BuildDerivationResponse,
)
from pynixd.path_tracker import PathTracker
from pynixd.scheduler import Scheduler
from pynixd.store_path import StorePath
from tests.functional.mock_store import MockStore

log = structlog.get_logger(__name__)


async def test_scheduler_flood_queueing():
    """Verify that the scheduler correctly queues jobs when all backends are full."""
    local_store = MockStore("local", feature_matrix={"x86_64-linux": set()})
    remote1 = MockStore("remote1", feature_matrix={"x86_64-linux": set()})
    remote2 = MockStore("remote2", feature_matrix={"x86_64-linux": set()})

    # idle=200, concurrency_penalty=100, min_schedule_score=1.0
    # 0 jobs: score 200
    # 1 job: score 100
    # 2 jobs: score 0 (below threshold)
    # Result: exactly 2 jobs per store.
    settings = PynixdSettings()
    settings.ranking.cpu_idle_weight = 200.0
    settings.ranking.concurrency_penalty = 100.0
    settings.ranking.min_schedule_score = 1.0
    settings.ranking.thundering_herd_penalty = (
        200.0  # prevent multiple in one pass to be safe
    )

    ctx = PynixdContext(
        settings=settings,
        local_store=local_store,
        stores={"remote1": remote1, "remote2": remote2},
        path_tracker=PathTracker(db=None),
    )
    scheduler = Scheduler(ctx)

    # Enqueue 10 builds
    futures = []
    blockers = {}
    for i in range(10):
        drv_path = StorePath(f"/nix/store/{i:032d}-test.drv")
        local_store.tracker.add_known_path(drv_path)

        # Block every build so we can inspect the state
        blockers[drv_path] = remote1.block_build(drv_path)
        remote2.block_build(drv_path, blocker=blockers[drv_path])
        local_store.block_build(drv_path, blocker=blockers[drv_path])

        req = BuildDerivationRequest(
            drv_path=drv_path,
            derivation=BasicDerivation(platform="x86_64-linux"),
        )
        # Use a dummy nar_hash to prevent the scheduler from refreshing/overwriting metadata
        info_map = {drv_path: UnkeyedValidPathInfo(nar_hash="dummy", nar_size=1024)}
        _bid, fut = await scheduler.queue.enqueue(
            req,
            None,
            required_paths=info_map,
            platform="x86_64-linux",
        )
        futures.append(fut)

    # Let the scheduler run a few passes
    for _ in range(10):
        await scheduler.schedule()
        await asyncio.sleep(0.05)
        # Wait until exactly 4 are in flight (2 per remote)
        pending = await scheduler.queue.get_pending()
        if len([b for b in pending if b.is_building]) >= 4:
            break

    # Inspect the queue
    pending = await scheduler.queue.get_pending()
    building = [b for b in pending if b.is_building]
    waiting = [b for b in pending if b.is_pending]

    log.info("initial_state", building=len(building), waiting=len(waiting))

    # Verify concurrency limits (2 remote1 + 2 remote2 = 4)
    assert len(building) == 4
    assert len(waiting) == 6

    # Complete 2 builds on remote1
    r1_building = [b for b in building if b.assigned_store_id == "remote1"]
    assert len(r1_building) == 2
    for b in r1_building:
        blockers[b.request.drv_path].set()

    # Wait for completion and next assignment
    for _ in range(10):
        await scheduler.schedule()
        await asyncio.sleep(0.05)
        pending = await scheduler.queue.get_pending()
        # Should still be 4 building (2 finished, 2 new ones picked up)
        if len([b for b in pending if b.is_building]) == 4:
            break

    pending = await scheduler.queue.get_pending()
    building = [b for b in pending if b.is_building]
    waiting = [b for b in pending if b.is_pending]

    log.info("after_two_done", building=len(building), waiting=len(waiting))

    # Still 4 building (2 slots were freed, 2 more were picked up)
    assert len(building) == 4
    assert len(waiting) == 4

    # Release EVERYTHING
    for b in blockers.values():
        if not b.is_set():
            b.set()

    # Repeatedly schedule until all are done
    for _ in range(20):
        await scheduler.schedule()
        await asyncio.sleep(0.05)
        pending = await scheduler.queue.get_pending()
        if len(pending) == 0:
            break

    # Wait for all futures to resolve
    await asyncio.wait_for(asyncio.gather(*futures), timeout=5.0)

    pending = await scheduler.queue.get_pending()
    assert len(pending) == 0


async def test_scheduler_locality_priority():
    """Verify that the scheduler prefers stores with better byte-weighted data locality."""
    local_store = MockStore("local", feature_matrix={"x86_64-linux": set()})
    remote_a = MockStore("remote_a", feature_matrix={"x86_64-linux": set()})
    remote_b = MockStore("remote_b", feature_matrix={"x86_64-linux": set()})

    settings = PynixdSettings()
    # Use defaults but ensure weights are high
    settings.ranking.locality_weight = 500.0
    settings.ranking.cpu_idle_weight = 100.0
    settings.ranking.concurrency_penalty = 50.0

    ctx = PynixdContext(
        settings=settings,
        local_store=local_store,
        stores={"remote_a": remote_a, "remote_b": remote_b},
        path_tracker=PathTracker(db=None),
    )
    scheduler = Scheduler(ctx)

    # Define a build with 1 big input and 1 small input
    big_path = StorePath("/nix/store/00000000000000000000000000000001-big")
    # remote_a has the big path, remote_b has nothing
    remote_a.tracker.add_known_path(big_path)
    local_store.tracker.add_known_path(big_path)

    drv_path = StorePath("/nix/store/00000000000000000000000000000002-test.drv")
    local_store.tracker.add_known_path(drv_path)

    # Use 1GB size in metadata. Set nar_hash to prevent the scheduler from
    # refreshing/overwriting with MockStore defaults.
    info_map = {
        big_path: UnkeyedValidPathInfo(nar_hash="big", nar_size=1024 * 1024 * 1024),
        drv_path: UnkeyedValidPathInfo(nar_hash="drv", nar_size=1024),
    }

    req = BuildDerivationRequest(
        drv_path=drv_path,
        derivation=BasicDerivation(platform="x86_64-linux"),
    )

    # Mock responders
    build_resp = BuildDerivationResponse(
        result=BuildResult(status=BuildResultStatus.BUILT),
    )
    remote_a.responses[BuildDerivationRequest] = build_resp
    remote_b.responses[BuildDerivationRequest] = build_resp

    # Block the build so it stays in building state
    done = remote_a.block_build(drv_path)
    remote_b.block_build(drv_path, blocker=done)

    # Enqueue
    _bid, fut = await scheduler.queue.enqueue(
        req,
        None,
        required_paths=info_map,
        platform="x86_64-linux",
    )

    await scheduler.schedule()

    # Wait for assignment
    pending = []
    for _ in range(10):
        pending = await scheduler.queue.get_pending()
        if pending and pending[0].assigned_store_id:
            break
        await asyncio.sleep(0.01)

    assert pending[0].assigned_store_id == "remote_a"

    # Cleanup
    done.set()
    await fut
