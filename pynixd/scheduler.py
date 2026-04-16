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
from collections.abc import Mapping, Iterator
from dataclasses import dataclass

import structlog

from .build_queue import BuildQueue, QueuedBuild
from .connection import ClientConn
from .exceptions import BackendError, InfrastructureError
from .operations.build_derivation import (
    BuildDerivationRequest,
    BuildDerivationResponse,
)

from .store import Store
from .store_path import StorePath

log = structlog.get_logger(__name__)


@dataclass
class RankedStore:
    store_id: str
    score: int
    slots: int
    store: Store


class RankedStores:
    def __init__(self, stores: list[RankedStore]) -> None:
        self._stores = stores

    def __iter__(self) -> Iterator[RankedStore]:
        return iter(self._stores)

    def __len__(self) -> int:
        return len(self._stores)

    def __bool__(self) -> bool:
        return bool(self._stores)

    def with_slots(self) -> RankedStores:
        return RankedStores([s for s in self._stores if s.slots > 0])

    def sort(self) -> RankedStores:
        return RankedStores(
            sorted(self._stores, key=lambda s: (s.score, s.slots), reverse=True)
        )


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
        request: BuildDerivationRequest,
        client: ClientConn | None,
        required_paths: set[StorePath],
        platform: str = "",
    ) -> tuple[int, asyncio.Future[BuildDerivationResponse]]:
        """Add a build to the queue and trigger the scheduler."""
        # 1. Fetch expected duration hint if DB is active
        hint = None
        if self.local_store.db:
            pname = request.derivation.env.get("pname", "")
            if pname:
                serialized = request.derivation.serialize_for_stats()
                hint = await self.local_store.db.get_build_stats_hint(
                    pname, platform, serialized
                )

        # 2. Enqueue with hint
        res = await self.queue.enqueue(
            request, client, required_paths, platform, expected_duration=hint
        )
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
        waiting_paths: list[QueuedBuild] = []
        building: list[int] = []
        transferring: list[int] = []

        for build in pending:
            if build.is_building:
                building.append(build.id)
                continue
            if build.is_transferring:
                transferring.append(build.id)
                continue

            # Check if all required paths are in local store
            if self.local_store.has_all_paths(build.required_paths):
                schedulable.append(build)
            else:
                waiting_paths.append(build)

        # 1.5 Sort schedulable builds by expected duration (Fast-track small builds)
        # Unknown duration (None) is treated as "medium" priority (infinity/2).
        def duration_key(b: QueuedBuild) -> float:
            if b.expected_duration is not None:
                return float(b.expected_duration)
            return 600000.0  # 10 minutes default for unknown

        schedulable.sort(key=duration_key)

        # 2. Assign schedulable builds to backends
        # Load balancing: prefer backends with the most relevant paths already present
        # and with free slots.
        waiting_slot: list[QueuedBuild] = []

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
            for rs in ranked.with_slots():
                if build.build_task is None:
                    log.debug(
                        "build_assigned_to_store",
                        build_id=build.id,
                        store_id=rs.store_id,
                        score=rs.score,
                        effective_slots=rs.slots,
                    )
                    build.build_task = asyncio.create_task(
                        self.execute_build(build, rs.store)
                    )
                    assigned = True
                    building.append(build.id)
                    break

            if not assigned:
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

            ranked = self.rank_stores(build)
            for rs in ranked.with_slots().sort():
                missing = build.required_paths - rs.store.known_paths
                if missing and self.local_store.has_all_paths(missing):
                    build.transfer_task = asyncio.create_task(
                        self.transfer_inputs(build, rs.store, missing)
                    )
                    transferring.append(build.id)
                    break

        log.debug(
            "scheduling_pass_done",
            pending=len(pending),
            building=len(building),
            transferring=len(transferring),
            waiting_paths=len(waiting_paths),
            waiting_slot=len(waiting_slot),
            slots={s.id: s.available_slots for s in self.stores.values()},
        )

    def rank_stores(self, build: QueuedBuild) -> RankedStores:
        """Rank stores for a build by path overlap, tiebreak by available slots."""
        stores = []
        for store_id, store in self.stores.items():
            if not store.is_healthy:
                continue
            if not store.supports_system(build.platform):
                continue
            if store_id in build.failed_backends:
                continue

            score = store.count_common_paths(build.required_paths)
            stores.append(RankedStore(store_id, score, store.available_slots, store))

        return RankedStores(stores).sort()

    async def execute_build(self, build: QueuedBuild, store: Store) -> None:
        """Execute build on a store, handling inputs and outputs."""
        build_resp: BuildDerivationResponse | None = None
        try:
            # Acquire build connection with semaphore FIRST to limit concurrency
            # This must be done before any async operations that might block
            async with store.build_conn() as conn:
                # 1. Ensure all inputs are present on the builder
                missing = build.required_paths - store.known_paths
                if missing:
                    log.debug(
                        "build_sending_inputs", build_id=build.id, store_id=store.id
                    )
                    await Store.stream_paths_store_to_store(
                        self.local_store, store, missing
                    )

                # 2. Trigger build
                log.debug("build_executing", build_id=build.id, store_id=store.id)
                build.started_at = time.monotonic()
                resp = await conn.call(build.request, client=build.client)
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
                    log.debug(
                        "pulled_paths_into_local_store",
                        count=len(outputs),
                        store_id=store.id,
                    )

                    # 4. Record build statistics
                    if self.local_store.db:
                        pname = build.request.derivation.env.get("pname")
                        if pname:
                            duration = int((time.monotonic() - build.started_at) * 1000)
                            await self.local_store.db.record_build_stats(
                                pname=pname,
                                version=build.request.derivation.env.get("version", ""),
                                platform=build.request.derivation.platform,
                                serialized_drv=build.request.derivation.serialize_for_stats(),
                                cpu_user_us=resp.result.cpu_user,
                                cpu_system_us=resp.result.cpu_system,
                                duration_ms=duration,
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
        self, build: QueuedBuild, store: Store, paths: set[StorePath]
    ) -> None:
        """Background task to proactively pull missing inputs for a build."""
        to_pull: set[StorePath] = set()
        try:
            to_pull = paths - self.local_store.known_paths
            if not to_pull:
                return

            log.info("pulling_paths", store_id=store.id, count=len(to_pull))
            for p in to_pull:
                log.debug("pulling_path", store_id=store.id, path=p)
            await Store.stream_paths_store_to_store(store, self.local_store, to_pull)
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
