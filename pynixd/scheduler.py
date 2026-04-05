"""
Scheduler for build distribution.

Runs scheduling passes triggered by:
- New builds enqueued
- Build completes (slot opens)
- Path transfer completes (availability changes)

DAG-aware: builds are only schedulable when all input_srcs are present
in the local store. If inputs are missing but available on a remote store,
they are pulled automatically.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import asyncssh
import structlog
from environs import Env

from .build_queue import BuildQueue, QueuedBuild
from .connection import ClientConn
from .exceptions import BackendError, InfrastructureError
from .operations.base import BuildResultStatus, PathInfo
from .operations.builds import (
    BuildDerivationRequest,
    BuildDerivationResponse,
)
from .protocol import Op
from .store import Store

log = structlog.get_logger(__name__)

env = Env()


class Scheduler:
    """Schedules builds across stores based on locality and DAG deps."""

    def __init__(
        self,
        stores: Mapping[str, Store],
        local_store: Store,
    ) -> None:
        self.queue = BuildQueue()
        self.stores = stores
        self.local_store = local_store
        self.trigger_event = asyncio.Event()
        self.running = False

    def trigger(self) -> None:
        """Signal that a scheduling pass is needed."""
        self.trigger_event.set()

    async def enqueue(
        self,
        op: Op,
        request: BuildDerivationRequest,
        client: ClientConn | None,
        required_paths: set[str],
        platform: str = "",
    ) -> tuple[int, asyncio.Future[BuildDerivationResponse]]:
        """Add a build to the queue and trigger the scheduler."""
        res = await self.queue.enqueue(op, request, client, required_paths, platform)
        self.trigger()
        return res

    async def start(self) -> None:
        """Start the scheduler loop."""
        self.running = True
        log.info("scheduler_started")
        while self.running:
            try:
                # Wait for something to happen
                await self.trigger_event.wait()
                self.trigger_event.clear()

                # Run a scheduling pass
                await self.schedule()

                # Wait a bit if we're spinning (shouldn't happen with Event)
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("scheduler_pass_failed")
                await asyncio.sleep(1.0)

    async def stop(self) -> None:
        """Stop the scheduler and cancel all pending builds."""
        self.running = False
        self.trigger()
        # TODO: Cancel in-flight builds if needed

    async def schedule(self) -> None:
        """The core scheduling logic.

        1. Identifies schedulable builds (inputs ready in local_store).
        2. Ranks backends for each build based on locality and load.
        3. Dispatches builds to available slots.
        4. Triggers proactive path pulling for builds waiting on DAG.
        """
        pending = await self.queue.get_pending()
        if not pending:
            return

        # 1. Identify builds ready to execute
        schedulable: list[QueuedBuild] = []
        waiting_dag: list[QueuedBuild] = []

        for build in pending:
            if build.is_building or build.is_transferring:
                continue

            # Check if all required paths are in local store
            if self.local_store.has_all_paths(build.required_paths):
                schedulable.append(build)
            else:
                waiting_dag.append(build)

        # 2. Assign schedulable builds to backends
        # Load balancing: prefer backends with the most relevant paths already present
        # and with free slots.
        waiting_slot: list[QueuedBuild] = []
        building: list[int] = []

        for build in schedulable:
            # Rank stores for this build
            ranked = self.rank_stores(build)

            # If NO store will ever support this platform, fail it statelessly
            if not ranked and not any(
                s.supports_system(build.platform) for s in self.stores.values()
            ):
                await self.queue.fail(
                    build.id, f"No store supports system: {build.platform}"
                )
                continue

            assigned = False
            for store_id, score in ranked:
                store = self.stores[store_id]
                if store.available_slots > 0:
                    log.debug(
                        "build_assigned_to_store",
                        build_id=build.id,
                        store_id=store_id,
                        score=score,
                        effective_slots=store.available_slots,
                    )
                    # Dispatch build!
                    build.build_task = asyncio.create_task(
                        self.execute_build(build, store)
                    )
                    assigned = True
                    building.append(build.id)
                    break

            if not assigned:
                waiting_slot.append(build)

        # 3. Handle proactive transfers for waiting_dag
        # We only start one proactive transfer task at a time to avoid
        # overwhelming the local store / network.
        transferring: list[int] = []
        for build in waiting_dag:
            if build.is_transferring:
                transferring.append(build.id)
                continue

            # Check if we should start a transfer for this build
            if len(transferring) < 2:  # Limit concurrent transfers
                # Which store has the missing paths?
                missing = build.required_paths - self.local_store.known_paths
                if not missing:
                    continue

                best_source = self.find_best_source(missing)
                if best_source:
                    build.transfer_task = asyncio.create_task(
                        self.transfer_inputs(build, best_source, missing)
                    )
                    transferring.append(build.id)

        log.debug(
            "scheduling_pass_done",
            total_builds=len(pending),
            building=building,
            transferring=transferring,
            waiting_dag=[b.id for b in waiting_dag if b.id not in transferring],
            waiting_slot=[b.id for b in waiting_slot],
            slots={s.id: s.available_slots for s in self.stores.values()},
        )

    def rank_stores(self, build: QueuedBuild) -> list[tuple[str, int]]:
        """Rank stores for a build. Score = present_paths - (penalty if busy)."""
        scores: list[tuple[str, int]] = []
        for store_id, store in self.stores.items():
            if not store.is_healthy:
                continue
            if not store.supports_system(build.platform):
                continue
            if store_id in build.failed_backends:
                continue

            # Base score: number of required paths already on this store
            score = store.count_common_paths(build.required_paths)

            # Busy penalty: prefer idle workers
            if store.available_slots == 0:
                score -= 1000

            scores.append((store_id, score))

        # Sort descending by score
        return sorted(scores, key=lambda x: x[1], reverse=True)

    def find_best_source(self, paths: set[str]) -> Store | None:
        """Find the store that has the most of the given paths."""
        best_store: Store | None = None
        max_count = -1

        for store in self.stores.values():
            if not store.is_healthy:
                continue
            count = store.count_common_paths(paths)
            if count > max_count:
                max_count = count
                best_store = store

        if max_count > 0:
            return best_store
        return None

    async def execute_build(self, build: QueuedBuild, store: Store) -> None:
        """Execute build on a store, handling inputs and outputs."""
        try:
            # 1. Ensure all inputs are present on the builder
            missing = build.required_paths - store.known_paths
            if missing:
                log.debug("build_sending_inputs", build_id=build.id, store_id=store.id)
                await store.pipe_paths_from(self.local_store, missing)

            # 2. Trigger build
            log.debug("build_executing", build_id=build.id, store_id=store.id)
            build.started_at = time.monotonic()
            resp = await store.execute(build.request, client=build.client)
            log.debug("build_executed", build_id=build.id, status=resp.result.status)

            # 3. Pull outputs back to local store if build succeeded
            if resp.result.status == 0:
                # Resolve drv to outputs
                outputs = build.request.derivation.output_paths()
                log.info("pulling_paths", store_id=store.id, count=len(outputs))
                for p in outputs.values():
                    log.debug("pulling_path", store_id=store.id, path=p)
                await self.local_store.pipe_paths_from(store, set(outputs.values()))
                log.debug(
                    "pulled_paths_into_local_store",
                    count=len(outputs),
                    store_id=store.id,
                )

            # 4. Finalize build
            await self.queue.complete(build.id, resp)
            self.trigger()

        except (BackendError, InfrastructureError) as e:
            log.warning("build_failed_retryable", build_id=build.id, error=str(e))
            # Don't fail the build yet, reset it for retry on another store
            # but we must stop any background transfer task first.
            await build.stop_transfer()
            build.reset_for_retry(store.id, build.transfer_task)
            self.trigger()
        except Exception:
            log.exception("build_crashed", build_id=build.id)
            await self.queue.fail(build.id, "Internal scheduler error")
            self.trigger()

    async def transfer_inputs(
        self, build: QueuedBuild, store: Store, paths: set[str]
    ) -> None:
        """Background task to proactively pull missing inputs for a build."""
        try:
            # Calculate which paths we *actually* need to pull (proactive)
            to_pull = paths - self.local_store.known_paths
            if not to_pull:
                return

            log.info("pulling_paths", store_id=store.id, count=len(to_pull))
            for p in to_pull:
                log.debug("pulling_path", store_id=store.id, path=p)
            try:
                # We pull from the build machine into our local store
                await self.local_store.pipe_paths_from(store, to_pull)
                log.debug(
                    "pulled_paths_into_local_store",
                    count=len(to_pull),
                    store_id=store.id,
                )
                self.trigger()
            except Exception:
                log.exception(
                    "pull_paths_failed",
                    count=len(to_pull),
                    store_id=store.id,
                )
        finally:
            build.transfer_task = None
