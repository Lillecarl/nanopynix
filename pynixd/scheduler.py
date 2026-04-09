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
import time
from collections.abc import Mapping

import structlog

from .build_queue import BuildQueue, QueuedBuild
from .connection import ClientConn
from .exceptions import BackendError, InfrastructureError
from .operations.build_derivation import (
    BuildDerivationRequest,
    BuildDerivationResponse,
)

from .protocol import Op
from .store import Store
from .store_path import RequiredInput

log = structlog.get_logger(__name__)


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
        required_paths: set[RequiredInput],
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

        # TODO TEMP DEBUG: Remove after debugging
        log.warning(
            "DEBUG_schedule_pending",
            total=len(pending),
            pending_build_ids=[b.id for b in pending],
            pending_building=[b.id for b in pending if b.is_building],
            pending_transferring=[b.id for b in pending if b.is_transferring],
            pending_done=[b.id for b in pending if b.is_done],
        )

        # 1. Identify builds ready to execute
        schedulable: list[QueuedBuild] = []
        waiting_dag: list[QueuedBuild] = []

        for build in pending:
            if build.is_building or build.is_transferring:
                # TODO TEMP DEBUG: Remove after debugging
                log.warning(
                    "DEBUG_skip_build",
                    build_id=build.id,
                    is_building=build.is_building,
                    is_transferring=build.is_transferring,
                    build_task_done=build.build_task.done()
                    if build.build_task
                    else None,
                    transfer_task_done=build.transfer_task.done()
                    if build.transfer_task
                    else None,
                )
                continue

            # Check if all required paths are in local store
            if self.local_store.has_all_paths(build.required_paths):
                schedulable.append(build)
            else:
                missing = build.required_paths - self.local_store.known_paths
                log.warning(
                    "DEBUG_waiting_paths",
                    build_id=build.id,
                    drv_path=str(build.request.drv_path),
                    missing_count=len(missing),
                    missing_paths=[repr(p) for p in missing][:10],
                )
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
                # TODO TEMP DEBUG: Remove after debugging
                log.warning(
                    "DEBUG_rank_store",
                    build_id=build.id,
                    store_id=store_id,
                    score=score,
                    available_slots=store.available_slots,
                    max_builds=store.max_builds,
                    existing_task=build.build_task is not None,
                )
                if store.available_slots > 0 and build.build_task is None:
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
                # TODO TEMP DEBUG: Remove after debugging
                log.warning(
                    "DEBUG_waiting_slot",
                    build_id=build.id,
                    ranked_stores=[
                        (sid, s.available_slots)
                        for sid, s in [(sid, self.stores[sid]) for sid, _ in ranked]
                    ],
                )
                waiting_slot.append(build)

        # 3. Handle proactive transfers for waiting_slot builds
        # Proactive transfer is for builds that are ready to run (all inputs in local_store)
        # but the best builder has no free slots. We transfer inputs to a builder with slots
        # so it can start building.
        transferring: list[int] = []
        for build in waiting_slot:
            if build.is_transferring:
                transferring.append(build.id)
                continue

            if len(transferring) < 2:
                # Re-rank stores to find one with slots (best store has no slots)
                ranked = self.rank_stores(build)
                for store_id, score in ranked:
                    store = self.stores[store_id]
                    if store.available_slots > 0:
                        # Found a store with slots
                        missing = build.required_paths - store.known_paths
                        if missing and store.has_all_paths(missing):
                            # Transfer inputs to this store then start build
                            build.transfer_task = asyncio.create_task(
                                self.transfer_inputs(build, store, missing)
                            )
                            transferring.append(build.id)
                        break

        log.debug(
            "scheduling_pass_done",
            total_builds=len(pending),
            building=building,
            transferring=transferring,
            waiting_dag=[b.id for b in waiting_dag if b.id not in transferring],
            waiting_slot=[b.id for b in waiting_slot],
            slots={s.id: s.available_slots for s in self.stores.values()},
        )

        # Re-trigger after 5s if there are pending builds that couldn't be scheduled
        # This handles cases where builds are waiting for slots to free up or for
        # DAG inputs to be pulled in by other builds.
        if waiting_slot or waiting_dag:

            async def retrigger_later():
                await asyncio.sleep(5.0)
                self.trigger()

            asyncio.create_task(retrigger_later())

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

    def find_best_source(self, paths: set[RequiredInput]) -> Store | None:
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
        # TODO TEMP DEBUG: Remove after debugging
        log.warning("DEBUG_execute_build_START", build_id=build.id, store_id=store.id)
        build_resp: BuildDerivationResponse | None = None
        try:
            # Acquire build connection with semaphore FIRST to limit concurrency
            # This must be done before any async operations that might block
            async with store.build_conn() as conn:
                # 1. Ensure all inputs are present on the builder
                missing = build.required_paths - store.known_paths
                if missing:
                    log.warning(
                        "DEBUG_build_missing_inputs",
                        build_id=build.id,
                        missing_count=len(missing),
                    )
                    log.debug(
                        "build_sending_inputs", build_id=build.id, store_id=store.id
                    )
                    await Store.stream_paths_store_to_store(
                        self.local_store, store, missing
                    )

                # 2. Trigger build
                log.warning(
                    "DEBUG_build_executing", build_id=build.id, store_id=store.id
                )
                log.debug("build_executing", build_id=build.id, store_id=store.id)
                build.started_at = time.monotonic()
                resp = await conn.call(build.request, client=build.client)
                log.warning(
                    "DEBUG_build_executed", build_id=build.id, status=resp.result.status
                )
                log.debug(
                    "build_executed", build_id=build.id, status=resp.result.status
                )

                # 3. Pull outputs back to local store if build succeeded
                if resp.result.status == 0:
                    # Resolve drv to outputs
                    outputs = build.request.derivation.output_paths()
                    store.add_known_paths(set(outputs.values()))
                    log.info("pulling_paths", store_id=store.id, count=len(outputs))
                    for p in outputs.values():
                        log.debug("pulling_path", store_id=store.id, path=p)
                    await Store.stream_paths_store_to_store(
                        store, self.local_store, set(outputs.values())
                    )
                    # IMPORTANT: Update local_store's known_paths after streaming
                    # because stream_paths_store_to_store bypasses the normal handle() path
                    self.local_store.add_known_paths(set(outputs.values()))
                    log.debug(
                        "pulled_paths_into_local_store",
                        count=len(outputs),
                        store_id=store.id,
                    )

                # Capture response for completion AFTER semaphore is released
                build_resp = resp

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

        # Release semaphore FIRST, then complete and trigger
        # This allows new builds to start while we're finalizing
        if build_resp is not None:
            await self.queue.complete(build.id, build_resp)
            self.trigger()

    async def transfer_inputs(
        self, build: QueuedBuild, store: Store, paths: set[RequiredInput]
    ) -> None:
        """Background task to proactively pull missing inputs for a build."""
        to_pull: set[RequiredInput] = set()
        try:
            to_pull = paths - self.local_store.known_paths
            if not to_pull:
                return

            log.info("pulling_paths", store_id=store.id, count=len(to_pull))
            for p in to_pull:
                log.debug("pulling_path", store_id=store.id, path=p)
            await Store.stream_paths_store_to_store(store, self.local_store, to_pull)
            # IMPORTANT: Update local_store's known_paths after streaming
            self.local_store.add_known_paths(to_pull)  # type: ignore[arg-type]
            log.debug(
                "pulled_paths_into_local_store",
                count=len(to_pull),
                store_id=store.id,
            )
            self.trigger()
        except ValueError as e:
            log.warning(
                "pull_paths_missing_inputs",
                build_id=build.id,
                error=str(e),
                store_id=store.id,
                count=len(to_pull),
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
