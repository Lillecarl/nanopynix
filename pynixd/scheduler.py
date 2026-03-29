"""
Scheduler for build distribution.

Runs scheduling passes triggered by:
- New builds enqueued
- Build completes (slot opens)
- Path transfer completes (availability changes)

DAG-aware: builds are only schedulable when all their required_paths
(input_srcs) exist on the local backend. Rankings are computed fresh
each pass (stateless).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import asyncssh

from .build_queue import BuildQueue, QueuedBuild
from .exceptions import BackendError, InfrastructureError
from .operations.base import PathInfo
from .operations.builds import (
    BuildDerivationRequest,
    BuildDerivationResponse,
)
from .store import Store

log: logging.Logger = logging.getLogger(__name__)

MAX_RETRIES: int = int(os.environ.get("PYNIXD_BUILD_RETRIES", "3"))
_PSI_PRESSURE_THRESHOLD: float = float(os.environ.get("PYNIXD_PSI_THRESHOLD", "70"))

# Build statuses that are worth retrying (possibly transient).
# PermanentFailure (3), InputRejected (4), OutputRejected (5),
# CachedFailure (7), DependencyFailed (12), NotDeterministic (14)
# are NOT retried — the build itself is broken.
_RETRYABLE_STATUSES: frozenset[int] = frozenset(
    {
        6,  # TransientFailure
        10,  # TimedOut
        11,  # MiscFailure
        13,  # LogLimitExceeded
    }
)


class Scheduler:
    """Schedules builds across stores based on locality and DAG deps."""

    def __init__(
        self,
        build_queue: BuildQueue,
        stores: dict[str, Store],
        local_store: Store,
    ) -> None:
        self._queue = build_queue
        self._stores = stores
        self._local_store = local_store
        self._trigger = asyncio.Event()
        self._running = False

    def trigger(self) -> None:
        """Signal that a scheduling pass is needed."""
        self._trigger.set()

    async def start(self) -> None:
        """Start the scheduler loop."""
        self._running = True
        log.info("Scheduler started")
        while self._running:
            await self._trigger.wait()
            self._trigger.clear()
            await self._run_scheduling_pass()

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False
        self._trigger.set()
        builds = await self._queue.get_pending()
        # Stop transfers gracefully, cancel builds hard (we're shutting down)
        for build in builds:
            if build.build_task and not build.build_task.done():
                build.build_task.cancel()
            build.transfer_cancel.set()
        tasks = []
        for build in builds:
            if build.build_task and not build.build_task.done():
                tasks.append(build.build_task)
            if build.is_transferring and build.transfer_task is not None:
                tasks.append(build.transfer_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Scheduling pass ─────────────────────────────────────────────

    def _is_schedulable(self, build: QueuedBuild) -> bool:
        """A build is schedulable when all required_paths exist on local."""
        return self._local_store.has_all_paths(build.required_paths)

    def _compute_ranking(self, build: QueuedBuild) -> list[tuple[str, int]]:
        """Rank stores by locality (common paths with required_paths).

        Filters out stores that don't support the build's platform,
        and lix stores for derivations that require nix features
        (dynamic drvs, CA floating, deferred, impure outputs).

        Stores where this build previously failed are deprioritized
        (sorted after non-failed stores at the same locality score).
        """
        needs_nix = build.request.derivation.requires_nix
        failed = set(build.failed_backends)
        return sorted(
            [
                (s.id, s.count_common_paths(build.required_paths))
                for s in self._stores.values()
                if s.supports_system(build.platform)
                and (not needs_nix or not s.is_lix)
                and s.is_healthy
            ],
            key=lambda x: (
                (self._stores[x[0]].pressure or 0) >= _PSI_PRESSURE_THRESHOLD,
                -x[1],
                x[0] in failed,
                self._stores[x[0]].pressure or 0.0,
            ),
        )

    def _effective_slots(
        self,
        store_id: str,
        assigned_this_pass: dict[str, int],
    ) -> int:
        store = self._stores[store_id]
        return store.available_slots - assigned_this_pass.get(store_id, 0)

    def _slots_exhausted(self, assigned_this_pass: dict[str, int]) -> bool:
        """True when all build slots are spoken for (in-use + assigned this pass)."""
        return self._total_available_slots() <= sum(assigned_this_pass.values())

    async def _sync_local_paths(self, builds: list[QueuedBuild]) -> None:
        """Ensure local_store knows about client-uploaded paths."""
        unknown: set[str] = set()
        for build in builds:
            if build.is_building or build.is_done:
                continue
            for path in build.required_paths:
                if not self._local_store.has_path(path):
                    unknown.add(path)
        if not unknown:
            return
        try:
            valid = await self._local_store.query_valid_paths(unknown)
            self._local_store.add_known_paths(valid)
            if unknown - valid:
                log.warning(
                    "sync_local_paths: %d/%d still missing: %s",
                    len(unknown - valid),
                    len(unknown),
                    sorted(unknown - valid)[:5],
                )
        except Exception:
            log.exception("Failed to sync %d local paths", len(unknown))

    def _total_available_slots(self) -> int:
        """Total free build slots across all healthy stores."""
        return sum(s.available_slots for s in self._stores.values() if s.is_healthy)

    async def _run_scheduling_pass(self) -> None:
        """Run one scheduling pass."""
        builds = await self._queue.get_pending()
        if not builds:
            return

        # Skip the pass if every slot is occupied — nothing can be assigned.
        # Builds completing will trigger a new pass when slots free up.
        has_pending = any(b.is_pending for b in builds)
        if has_pending and self._slots_exhausted({}):
            return

        # Sync local paths so we know about client-uploaded paths
        await self._sync_local_paths(builds)

        # Categorize builds for logging
        building: list[int] = []
        transferring: list[int] = []
        waiting_dag: list[int] = []
        waiting_slot: list[int] = []  # filled during assignment loop

        for b in builds:
            if b.is_building:
                building.append(b.id)
            elif not b.is_pending:
                pass  # done, shouldn't be here
            elif not self._compute_ranking(b):
                # No compatible store — fail immediately
                needs_nix = b.request.derivation.requires_nix
                all_systems = sorted(
                    {s for s in self._stores.values() for s in s.supported_systems}
                )
                reason = (
                    f"No compatible store for {b.description} "
                    f"(platform={b.platform}, requires_nix={needs_nix}, "
                    f"stores: {', '.join(all_systems) or 'any (unconstrained)'})"
                )
                await self._queue.fail(b.id, reason)
            elif not self._is_schedulable(b):
                waiting_dag.append(b.id)
            # else: schedulable pending — will be waiting_slot if not assigned

        slots = {s.id: s.available_slots for s in self._stores.values()}
        assigned_this_pass: dict[str, int] = {}

        for build in builds:
            if build.is_building or build.is_done:
                continue

            if not self._is_schedulable(build):
                continue  # already categorized as waiting_dag

            # No slots left anywhere — stop trying to assign
            if self._slots_exhausted(assigned_this_pass):
                # Mark all remaining pending builds as waiting_slot
                for remaining in builds:
                    if remaining.is_pending and self._is_schedulable(remaining):
                        if (
                            remaining.id not in waiting_slot
                            and remaining.id != build.id
                        ):
                            waiting_slot.append(remaining.id)
                waiting_slot.append(build.id)
                break

            ranking = self._compute_ranking(build)
            if not ranking:
                continue  # already failed above

            top_score = ranking[0][1]
            tied_top_with_slot = [
                (sid, score)
                for sid, score in ranking
                if score == top_score
                and self._effective_slots(sid, assigned_this_pass) > 0
            ]

            if tied_top_with_slot:
                # Pick least-loaded among tied-top
                best = min(
                    tied_top_with_slot,
                    key=lambda x: self._stores[x[0]].in_flight,
                )
                log.info(
                    "Build %d -> %s (score=%d, slots=%d)",
                    build.id,
                    best[0],
                    best[1],
                    self._effective_slots(best[0], assigned_this_pass),
                )
                self._start_build(build, self._stores[best[0]])
                assigned_this_pass[best[0]] = assigned_this_pass.get(best[0], 0) + 1
            else:
                waiting_slot.append(build.id)
                # Start proactive transfer to best store WITH a slot
                if not build.is_transferring:
                    for sid, _score in ranking:
                        store = self._stores[sid]
                        if (
                            self._effective_slots(sid, assigned_this_pass) > 0
                            and store.available_transfer_slots > 0
                        ):
                            self._start_transfer(build, store)
                            transferring.append(build.id)
                            break

        # Re-trigger if builds are waiting for paths — they may arrive later
        if waiting_dag:
            asyncio.get_event_loop().call_later(1.0, self.trigger)

        unhealthy = [s.id for s in self._stores.values() if not s.is_healthy]

        log.info(
            "Scheduling pass done: %d total "
            "(building=%s, transferring=%s, waiting_dag=%s, waiting_slot=%s), "
            "slots=%s",
            len(builds),
            building,
            transferring,
            waiting_dag,
            waiting_slot,
            slots,
        )
        if unhealthy:
            log.warning("Stores in cooldown: %s", unhealthy)

        pressure = {
            s.id: f"{s.pressure:.1f}"
            for s in self._stores.values()
            if s.pressure is not None
        }
        if pressure:
            log.info("Backend pressure: %s", pressure)

        memory = {
            s.id: f"{s.meminfo.available_mb}MB/{s.meminfo.total_mb}MB"
            for s in self._stores.values()
            if s.meminfo is not None
        }
        if memory:
            log.info("Backend memory: %s", memory)

    # ── Build lifecycle ─────────────────────────────────────────────

    def _start_build(self, build: QueuedBuild, store: Store) -> None:
        """Assign a build to a store and spawn its execution task."""
        build.started_at = time.monotonic()
        build.build_task = asyncio.create_task(
            self._execute_build(build, store),
        )

    def _start_transfer(self, build: QueuedBuild, store: Store) -> None:
        """Start a proactive transfer to a store."""
        log.info(
            "Build %d: starting proactive transfer to %s",
            build.id,
            store.id,
        )
        build.transfer_task = asyncio.create_task(
            self._do_proactive_transfer(build, store),
        )

    def _should_retry(
        self,
        build: QueuedBuild,
        status: int | None,
        store: Store,
    ) -> bool:
        """Decide if a failed build should be retried.

        status=None means infrastructure failure (exception before build ran).
        """
        if build.retries >= MAX_RETRIES:
            return False
        if status is None:
            return True  # infrastructure failure — always retry
        return status in _RETRYABLE_STATUSES

    async def _retry_build(self, build: QueuedBuild, store: Store) -> None:
        """Reset build state for retry on next scheduling pass."""
        await build.stop_transfer()
        build.retries += 1
        build.failed_backends.append(store.id)
        build.build_task = None
        build.started_at = None
        build.transfer_task = None
        build.transfer_cancel = asyncio.Event()
        log.info(
            "Build %d: retry %d/%d (failed on %s)",
            build.id,
            build.retries,
            MAX_RETRIES,
            store.id,
        )

    async def _execute_build(
        self,
        build: QueuedBuild,
        store: Store,
    ) -> None:
        """Execute a build on a store (runs as a spawned task).

        Acquires its own local_store connection so the build survives
        client proxy disconnect.
        """
        try:
            # Stop proactive transfer gracefully before acquiring connections
            await build.stop_transfer()

            log.debug("Build %d: sending inputs to %s", build.id, store.id)
            # Transfer required inputs
            await self._ensure_worker_has_inputs(build, store)

            log.debug("Build %d: executing on %s", build.id, store.id)
            assert isinstance(build.request, BuildDerivationRequest), (
                f"Build {build.id}: expected BuildDerivationRequest, "
                f"got {type(build.request).__name__}"
            )
            response = await self._execute_build_derivation(
                build,
                store,
            )

            if response.result.status not in (0, 1, 2):
                log.warning(
                    "Unexpected build status=%d: %s",
                    response.result.status,
                    response.result.error_msg,
                )

            if response.result.status != 0 and self._should_retry(
                build,
                response.result.status,
                store,
            ):
                if response.result.status in _RETRYABLE_STATUSES:
                    store.record_failure()
                await self._retry_build(build, store)
                return

            if response.result.status == 0:
                store.record_success()

            log.debug("Build %d: completing", build.id)
            await self._queue.complete(build.id, response)
            log.debug("Build %d completed", build.id)

        except (
            TimeoutError,
            InfrastructureError,
            BackendError,
            OSError,
            EOFError,
            ConnectionError,
            asyncssh.Error,
        ) as e:
            store.record_failure()
            if self._should_retry(build, None, store):
                await self._retry_build(build, store)
            else:
                log.exception(
                    "Build %d failed on %s (no retries left)", build.id, store.id
                )
                await self._queue.fail(
                    build.id,
                    f"Build failed after {build.retries} retries "
                    f"(last: {store.id}): {e}",
                )
        except Exception as e:
            # Programming error — don't retry, don't blame the store
            log.exception("Build %d: unexpected error", build.id)
            await self._queue.fail(build.id, f"Internal error: {type(e).__name__}: {e}")

        finally:
            self.trigger()

    async def _execute_build_derivation(
        self,
        build: QueuedBuild,
        store: Store,
    ) -> BuildDerivationResponse:
        """Execute a BuildDerivation on a store."""
        assert isinstance(build.request, BuildDerivationRequest)
        response = await store.build_derivation(
            build.request,
            client=build.client,
            suppress_last=True,
        )
        log.debug(
            "Build %d executed, status=%d",
            build.id,
            response.result.status,
        )

        # Pull outputs to local store
        if response.result.status == 0:
            built = response.result.built_outputs
            if built:
                # CA derivations: extract paths from realisations
                output_paths = []
                for name, realisation in built.items():
                    p = realisation.get("outPath", "")
                    if p and not p.startswith("/nix/store/"):
                        p = f"/nix/store/{p}"
                    if p:
                        output_paths.append(p)
                    else:
                        log.warning("Built output %s has no path", name)
            else:
                # Input-addressed: paths known from derivation
                output_paths = [
                    o.path for o in build.request.derivation.outputs if o.path
                ]
            log.info(
                "Build %d succeeded, pulling %d outputs: %s",
                build.id,
                len(output_paths),
                output_paths,
            )
            await self._pull_paths(store, output_paths)

        return response

    # ── Proactive transfer ──────────────────────────────────────────

    async def _do_proactive_transfer(
        self,
        build: QueuedBuild,
        store: Store,
    ) -> None:
        """Transfer paths to a store one at a time.

        Sends paths individually so that:
        - Cancellation can happen between paths (when a slot opens)
        - Partially transferred closures still leave useful paths on the store
        """
        try:
            sorted_paths, infos = await self._compute_transfer_plan(
                build,
                store,
            )
            if not sorted_paths:
                return

            transferred = 0
            for path in sorted_paths:
                # Check for graceful cancellation between paths
                if build.transfer_cancel.is_set():
                    log.info(
                        "Build %d: proactive transfer to %s stopped "
                        "after %d/%d paths (build starting)",
                        build.id,
                        store.id,
                        transferred,
                        len(sorted_paths),
                    )
                    return

                try:
                    await store.stream_paths_store_to_store(
                        src=self._local_store,
                        paths_with_info=[(path, infos[path])],
                    )
                    store.add_known_path(path)
                    transferred += 1
                except Exception:
                    log.debug(
                        "Proactive transfer of %s to %s failed, skipping",
                        path,
                        store.id,
                    )

            log.info(
                "Build %d: proactive transfer to %s done (%d/%d paths sent)",
                build.id,
                store.id,
                transferred,
                len(sorted_paths),
            )
            self.trigger()
        except Exception:
            log.exception(
                "Proactive transfer to %s failed for build %d",
                store.id,
                build.id,
            )
            self.trigger()

    # ── Path transfer helpers ───────────────────────────────────────

    async def _ensure_closure(self, build: QueuedBuild) -> set[str]:
        """Return the cached runtime closure, computing it on first call.

        The closure only depends on required_paths and the local store's
        reference graph — both stable once the build is schedulable.
        """
        if build.closure is not None:
            return build.closure

        seeds = build.required_paths
        db = self._local_store.db
        closure: set[str] | None = None
        if db is not None:
            closure = await db.compute_closure(seeds)

        if closure is None:
            # Slow path: walk references sequentially
            closure = set(seeds)
            queue = list(seeds)
            while queue:
                path = queue.pop()
                try:
                    path_info = await self._local_store.query_path_info(path)
                    if path_info:
                        for ref in path_info.references:
                            if ref not in closure:
                                closure.add(ref)
                                queue.append(ref)
                except Exception:
                    pass

        log.debug(
            "Closure expanded from %d to %d paths",
            len(seeds),
            len(closure),
        )
        build.closure = closure
        return closure

    async def _compute_transfer_plan(
        self,
        build: QueuedBuild,
        store: Store,
    ) -> tuple[list[str], dict[str, PathInfo]]:
        """Compute which paths a worker is missing and return them topo-sorted.

        Uses the build's cached closure (computed once, reused across workers).

        Returns:
            (sorted_paths, infos) — sorted_paths in topo order (deps first),
            infos maps path -> PathInfo. Both empty if nothing is missing.
        """
        if not build.required_paths:
            return [], {}

        closure = await self._ensure_closure(build)

        # Diff against worker using known_paths (we control all transfers)
        missing = closure - store.known_paths
        if not missing:
            return [], {}

        log.info(
            "Worker %s missing %d/%d paths",
            store.id,
            len(missing),
            len(closure),
        )

        # Batch PathInfo for missing paths
        infos = await self._local_store.query_path_infos(missing)

        if not infos:
            log.debug(
                "No transferable paths for missing inputs (paths may exist on host)",
            )
            return [], {}

        return self._topo_sort(infos), infos

    @staticmethod
    def _topo_sort(infos: dict[str, PathInfo]) -> list[str]:
        """Topological sort: dependencies before dependents."""
        sorted_paths: list[str] = []
        visited: set[str] = set()

        def visit(path: str) -> None:
            if path in visited or path not in infos:
                return
            visited.add(path)
            for ref in infos[path].references:
                visit(ref)
            sorted_paths.append(path)

        for path in infos:
            visit(path)

        return sorted_paths

    async def _ensure_worker_has_inputs(
        self,
        build: QueuedBuild,
        store: Store,
    ) -> None:
        """Send missing paths from local store to a worker."""
        sorted_paths, infos = await self._compute_transfer_plan(build, store)
        if not sorted_paths:
            return

        to_send = [(p, infos[p]) for p in sorted_paths]
        await store.stream_paths_store_to_store(
            src=self._local_store, paths_with_info=to_send
        )
        store.add_known_paths(set(sorted_paths))
        log.info(
            "Sent %d missing inputs to worker %s",
            len(to_send),
            store.id,
        )

    async def _pull_paths(
        self,
        store: Store,
        paths: list[str],
    ) -> None:
        """Pull store paths from store into pynixd's local store."""
        if not paths:
            return

        log.info("Pulling %d paths from %s", len(paths), store.id)

        # Query PathInfo for all paths concurrently
        async def query_one(path: str) -> tuple[str, PathInfo] | None:
            try:
                path_info = await store.query_path_info(path)
                if path_info is None:
                    log.warning("Path %s not valid on store %s", path, store.id)
                    return None
                path_info.path = path
                return (path, path_info)
            except Exception:
                log.exception("Failed to query PathInfo for %s on %s", path, store.id)
                return None

        results = await asyncio.gather(*[query_one(path) for path in paths])
        to_pull: list[tuple[str, PathInfo]] = [r for r in results if r is not None]

        if not to_pull:
            return

        try:
            await self._local_store.stream_paths_store_to_store(
                src=store, paths_with_info=to_pull
            )
            for path, _ in to_pull:
                self._local_store.add_known_path(path)
                store.add_known_path(path)
            log.info("Pulled %d paths into local store", len(to_pull))
        except Exception:
            log.exception(
                "Failed to pull %d paths from %s",
                len(to_pull),
                store.id,
            )
