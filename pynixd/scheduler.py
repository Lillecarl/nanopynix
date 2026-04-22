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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from .build_queue import BuildQueue, QueuedBuild
from .connection import ClientConn
from .derived_path import DerivedPath
from .exceptions import BackendError, InfrastructureError, ResourceExhaustedError
from .operations.base import BuildMode
from .operations.build_derivation import (
    BuildDerivationRequest,
    BuildDerivationResponse,
)
from .operations.build_paths import BuildPathsWithResultsResponse

from .store import Store
from .store_path import StorePath
from .stderr import StderrNext

from .allocator import BuildAllocator, TINY_BUILD_THRESHOLD_MS
from .decomposer import BuildDecomposer
from .dynamic_resolver import DynamicDerivationResolver
from . import metrics

if TYPE_CHECKING:
    from .drv_parser import ParsedDerivation
    from .context import PynixdContext

log = structlog.get_logger(__name__)

DerivationReader = Callable[[Path, "StorePath"], "ParsedDerivation"]


class Scheduler:
    """Schedules builds across stores based on locality and DAG deps."""

    def __init__(
        self,
        ctx: PynixdContext,
        read_drv_fn: DerivationReader | None = None,
    ) -> None:
        self.ctx = ctx
        self.queue = BuildQueue()
        self.stores = ctx.stores
        self.local_store = ctx.local_store

        self.allocator = BuildAllocator(self.stores, self.local_store)
        self.decomposer = BuildDecomposer(self, read_drv_fn=read_drv_fn)
        self.dynamic_resolver = DynamicDerivationResolver(self, read_drv_fn=read_drv_fn)
        self.trigger_event = asyncio.Event()
        self.running = False
        self.last_activity_at: float = asyncio.get_event_loop().time()

    def record_activity(self) -> None:
        """Update last activity timestamp."""
        self.last_activity_at = asyncio.get_event_loop().time()

    def trigger(self) -> None:
        """Signal that a scheduling pass is needed."""
        self.trigger_event.set()

    def add_store(self, store_id: str, store: Store) -> None:
        """Dynamically add a new store to the scheduler."""
        log.info("adding_store_dynamically", store_id=store_id)
        # We need to update both our mapping and the allocator's mapping
        # Since self.stores is often passed as a dict to __init__,
        # we ensure it's a mutable dict.
        if not isinstance(self.stores, dict):
            self.stores = dict(self.stores)
        self.stores[store_id] = store
        self.allocator.stores = self.stores
        self.trigger()

    async def remove_store(self, store_id: str, drain_timeout: float = 300.0) -> None:
        """Gracefully drain and remove a store."""
        store = self.stores.get(store_id)
        if not store:
            return

        log.info(
            "removing_store_dynamically", store_id=store_id, drain_timeout=drain_timeout
        )
        store.draining = True
        self.trigger()

        # 1. Graceful Drain Phase
        # Wait for all build slots to be released
        start = time.monotonic()
        while time.monotonic() - start < drain_timeout:
            if store.build_semaphore._value == store.max_builds:
                break
            await asyncio.sleep(1.0)
        else:
            log.warning("drain_timeout_reached", store_id=store_id)

        # 2. Hard-Kill Phase
        # Find all builds currently assigned to this store and cancel them
        pending = await self.queue.get_pending()
        for build in pending:
            if build.assigned_store_id == store_id:
                if build.build_task and not build.build_task.done():
                    log.info(
                        "cancelling_build_for_store_removal",
                        build_id=build.id,
                        store_id=store_id,
                    )
                    build.build_task.cancel()
                if build.transfer_task and not build.transfer_task.done():
                    build.transfer_task.cancel()

                # Requeue: reset state so it can be picked up by another store
                build.reset_for_retry(store_id, build.transfer_task)

        # 3. Final Removal
        if not isinstance(self.stores, dict):
            self.stores = dict(self.stores)
        self.stores.pop(store_id, None)
        self.allocator.stores = self.stores
        await store.close()
        log.info("store_removed_dynamically", store_id=store_id)
        self.trigger()

    async def build_derivation(
        self,
        request: BuildDerivationRequest,
        client: ClientConn | None,
        required_paths: set[StorePath],
        platform: str = "",
        scheduler_request_id: int | None = None,
        derived_paths_for_request: set[DerivedPath] | None = None,
    ) -> tuple[int, asyncio.Future[BuildDerivationResponse]]:
        """Add a build to the queue and trigger the scheduler."""
        hint = None
        if self.local_store.db:
            pname = request.derivation.env.get("pname", None)
            if pname:
                serialized = request.derivation.serialize_for_stats()
                hint = await self.local_store.db.get_build_stats_hint(
                    pname, platform, serialized
                )

        res = await self.queue.enqueue(
            request,
            client,
            required_paths,
            platform,
            expected_duration=hint,
            scheduler_request_id=scheduler_request_id,
            derived_paths_for_request=derived_paths_for_request,
        )
        self.trigger()
        return res

    async def build_derived_paths(
        self,
        derived_paths: set[DerivedPath],
        build_mode: BuildMode,
        client: ClientConn | None = None,
    ) -> BuildPathsWithResultsResponse:
        """Decompose DerivedPath set into individual builds and execute them."""
        return await self.decomposer.decompose(derived_paths, build_mode, client)

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

    async def close(self) -> None:
        """Alias for stop() for consistency."""
        await self.stop()

    async def schedule(self) -> None:
        """The core scheduling logic.

        1. Identifies schedulable builds (inputs ready in local_store).
        2. Ranks backends for each build based on locality and load.
        3. Dispatches builds to available slots.
        4. Triggers proactive path pulling for builds waiting on DAG.
        """
        pending = await self.queue.get_pending()
        if not pending:
            # Still update store metrics even if queue is empty
            for s in self.stores.values():
                metrics.STORE_AVAILABLE_SLOTS.labels(store_id=s.id).set(
                    s.available_slots
                )
                metrics.STORE_HEALTHY.labels(store_id=s.id).set(
                    1 if s.is_healthy else 0
                )
                if s.cpu_util:
                    metrics.STORE_CPU_UTILIZATION.labels(store_id=s.id).set(
                        s.cpu_util.utilization
                    )
            return

        # 1. Identify builds ready to execute
        schedulable: list[QueuedBuild] = []
        waiting_paths: list[QueuedBuild] = []
        waiting_deps: list[QueuedBuild] = []
        building: list[int] = []
        transferring: list[int] = []

        for build in pending:
            if build.is_building:
                building.append(build.id)
                continue
            if build.is_transferring:
                transferring.append(build.id)
                continue

            # Check if all DAG dependencies are satisfied
            if build.depends_on:
                unfinished_deps = {
                    dep_id
                    for dep_id in build.depends_on
                    if dep_id in self.queue.by_id
                    and not self.queue.by_id[dep_id].is_done
                }
                if unfinished_deps:
                    waiting_deps.append(build)
                    continue

            # Check if all required paths are in local store
            if self.local_store.tracker.has_all_paths(build.required_paths):
                schedulable.append(build)
            else:
                waiting_paths.append(build)

        # 2. Assign schedulable builds to backends
        # Load balancing: prefer backends with the most relevant paths already present
        # and with free slots.
        waiting_slot: list[QueuedBuild] = []

        for build in schedulable:
            # 1. Check for "Tiny Build" fast-track to local store
            # We only do this if it's explicitly tiny, not just unknown.
            build_features = build.request.derivation.effective_required_features
            if (
                build.expected_duration is not None
                and build.expected_duration <= TINY_BUILD_THRESHOLD_MS
                and self.local_store.supports_derivation(build.platform, build_features)
            ):
                # Is local store available? (We don't want to swamp it either)
                # But tiny builds are "free" enough that we can be liberal.
                if self.local_store.available_slots > 0:
                    log.info(
                        "build_fasttracked_local",
                        build_id=build.id,
                        duration=build.expected_duration,
                    )
                    metrics.QUEUE_SIZE.labels(status="pending").dec()
                    metrics.QUEUE_SIZE.labels(status="building").inc()
                    if build.wait_time is not None:
                        metrics.QUEUE_WAIT_DURATION.observe(build.wait_time)
                    build.build_task = asyncio.create_task(
                        self.execute_build(build, self.local_store)
                    )
                    building.append(build.id)
                    continue

            # 2. Standard remote backend assignment
            ranked = self.allocator.rank_stores(build)

            # If NO store will ever support this platform/features, fail it statelessly
            if not ranked and not any(
                s.supports_derivation(
                    build.platform, build.request.derivation.effective_required_features
                )
                for s in self.stores.values()
            ):
                reasons = self.allocator.incompatibility_reasons(
                    build.platform, build_features
                )
                error_msg = (
                    f"No compatible store for {build.platform}"
                    + (
                        f" (requires {', '.join(sorted(build_features))})"
                        if build_features
                        else ""
                    )
                    + "\n"
                    + "\n".join(f"  {r}" for r in reasons)
                )
                client = await self.queue.fail(build.id, error_msg)
                if client is not None:
                    for line in error_msg.split("\n"):
                        client.queue.put_nowait(StderrNext(text=f"pynixd: {line}\n"))
                if build.scheduler_request_id is not None:
                    await self.dynamic_resolver.on_build_complete_failed(
                        build, error_msg
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
                    metrics.QUEUE_SIZE.labels(status="pending").dec()
                    metrics.QUEUE_SIZE.labels(status="building").inc()
                    # wait_time is available now because build.started_at will be set in execute_build
                    # but we can observe it there too. Actually build_queue uses MONOTONIC.
                    build.build_task = asyncio.create_task(
                        self.execute_build(build, rs.store)
                    )
                    assigned = True
                    building.append(build.id)
                    break

            if not assigned:
                # If all compatible stores have already failed this build,
                # it's permanently stuck — fail it now with a clear message.
                compatible = [
                    s
                    for s in self.stores.values()
                    if s.supports_derivation(build.platform, build_features)
                ]
                if compatible and all(
                    s.id in build.failed_backends for s in compatible
                ):
                    failed_ids = [s.id for s in compatible]
                    error_msg = (
                        f"All compatible stores failed for {build.platform}"
                        + (
                            f" (requires {', '.join(sorted(build_features))})"
                            if build_features
                            else ""
                        )
                        + f": {', '.join(failed_ids)}"
                    )
                    client = await self.queue.fail(build.id, error_msg)
                    if client is not None:
                        for line in error_msg.split("\n"):
                            client.queue.put_nowait(
                                StderrNext(text=f"pynixd: {line}\n")
                            )
                    if build.scheduler_request_id is not None:
                        await self.dynamic_resolver.on_build_complete_failed(
                            build, error_msg
                        )
                    continue
                waiting_slot.append(build)

        # 3. Handle proactive transfers for waiting_slot builds
        # Proactive transfer is for builds that are ready to run (all inputs in local_store)
        # but the best builder has no free slots. We transfer inputs to a builder with slots
        # so it can start building.
        transferring_list: list[int] = []
        for build in waiting_slot:
            if build.is_transferring:
                transferring_list.append(build.id)
                continue

            ranked = self.allocator.rank_stores(build)
            for rs in ranked.with_slots().sort():
                missing = build.required_paths - rs.store.tracker.known_paths
                if missing and self.local_store.tracker.has_all_paths(missing):
                    build.transfer_task = asyncio.create_task(
                        self.transfer_inputs(build, rs.store, missing)
                    )
                    transferring_list.append(build.id)
                    break

        # 4. Update store metrics
        for s in self.stores.values():
            metrics.STORE_AVAILABLE_SLOTS.labels(store_id=s.id).set(s.available_slots)
            metrics.STORE_HEALTHY.labels(store_id=s.id).set(1 if s.is_healthy else 0)
            if s.cpu_util:
                metrics.STORE_CPU_UTILIZATION.labels(store_id=s.id).set(
                    s.cpu_util.utilization
                )

        log.debug(
            "scheduling_pass_done",
            pending=len(pending),
            building=len(building),
            transferring=len(transferring_list),
            waiting_paths=len(waiting_paths),
            waiting_deps=len(waiting_deps),
            waiting_slot=len(waiting_slot),
            slots={s.id: s.available_slots for s in self.stores.values()},
            cpu_util={
                s.id: f"{s.cpu_util.utilization:.1f}%" if s.cpu_util else None
                for s in self.stores.values()
            },
        )

    async def execute_build(self, build: QueuedBuild, store: Store) -> None:
        """Execute build on a store, handling inputs and outputs."""
        build.assigned_store_id = store.id
        build_resp: BuildDerivationResponse | None = None
        try:
            # Acquire build connection with semaphore FIRST to limit concurrency
            # This must be done before any async operations that might block
            async with store.build_conn() as conn:
                # 0. Register CA realisations from completed dependency builds
                # on the target builder store so it can resolve deferred
                # derivation output paths.
                if build.depends_on:
                    await self.dynamic_resolver.register_dep_realisations(build, store)
                    await self.dynamic_resolver.resolve_deferred_derivation(
                        build, store
                    )
                    await self.dynamic_resolver.resolve_dynamic_derivation(build, store)

                # Strip pynixd-handled features from requiredSystemFeatures.
                # After resolution, the backend daemon doesn't need to see
                # features like ca-derivations — pynixd already converted the
                # derivation to a regular InputAddressed BuildDerivation.
                self.allocator.strip_handled_features(build)

                # 1. Ensure all inputs are present on the builder
                missing = build.required_paths - store.tracker.known_paths
                if missing:
                    log.debug(
                        "build_sending_inputs", build_id=build.id, store_id=store.id
                    )
                    await self.local_store.stream_paths_to(store, missing)

                # 2. Trigger build
                log.debug("build_executing", build_id=build.id, store_id=store.id)
                build.started_at = time.monotonic()
                if build.wait_time is not None:
                    metrics.QUEUE_WAIT_DURATION.observe(build.wait_time)
                resp = await conn.call(build.request, client=build.client)
                log.debug(
                    "build_executed", build_id=build.id, status=resp.result.status
                )

                # 3. Pull outputs back to local store if build succeeded
                if resp.result.status == 0:
                    ca_output_paths: set[StorePath] = set()
                    if resp.result.built_outputs:
                        for (
                            drv_output_str,
                            realisation,
                        ) in resp.result.built_outputs.items():
                            out_path = realisation.get("outPath")
                            if out_path:
                                ca_output_paths.add(
                                    StorePath(out_path).with_store_prefix()
                                )
                        build.ca_realisations = list(resp.result.built_outputs.values())

                    outputs = build.request.derivation.output_paths()
                    static_paths = {p for p in outputs.values() if p != StorePath("")}
                    all_output_paths = static_paths | ca_output_paths
                    store.tracker.add_known_paths(all_output_paths)
                    log.info(
                        "pulling_paths", store_id=store.id, count=len(all_output_paths)
                    )
                    for p in all_output_paths:
                        log.debug("pulling_path", store_id=store.id, path=p)
                    await store.stream_paths_to(self.local_store, all_output_paths)
                    log.debug(
                        "pulled_paths_into_local_store",
                        count=len(all_output_paths),
                        store_id=store.id,
                    )

                    # Register CA realisations after outputs are in local store
                    await self.dynamic_resolver.register_built_outputs(build, resp)

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

        except ResourceExhaustedError as e:
            log.info(
                "build_deferred_busy",
                build_id=build.id,
                store_id=store.id,
                reason=str(e),
            )
            await build.stop_transfer()
            build.reset_for_busy(build.transfer_task)
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
            if build.scheduler_request_id is not None:
                await self.dynamic_resolver.on_build_complete_failed(
                    build, "Internal scheduler error"
                )
            self.trigger()

        # Release semaphore FIRST, then complete and trigger
        # This allows new builds to start while we're finalizing
        if build_resp is not None:
            await self.queue.complete(build.id, build_resp)
            if build.scheduler_request_id is not None:
                await self.dynamic_resolver.on_build_complete(build, build_resp)
            self.trigger()

    async def transfer_inputs(
        self, build: QueuedBuild, store: Store, paths: set[StorePath]
    ) -> None:
        """Background task to proactively pull missing inputs for a build."""
        to_pull: set[StorePath] = set()
        try:
            to_pull = paths - self.local_store.tracker.known_paths
            if not to_pull:
                return

            log.info("pulling_paths", store_id=store.id, count=len(to_pull))
            for p in to_pull:
                log.debug("pulling_path", store_id=store.id, path=p)
            await store.stream_paths_to(self.local_store, to_pull)
            metrics.PATHS_TRANSFERRED.labels(
                source=store.id, destination=self.local_store.id
            ).inc(len(to_pull))
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
