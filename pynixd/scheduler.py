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
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto

import asyncssh
import structlog
from environs import Env

from .build_queue import BuildQueue, QueuedBuild
from .exceptions import BackendError, InfrastructureError
from .operations.base import BuildResultStatus, PathInfo
from .operations.builds import (
    BuildDerivationRequest,
    BuildDerivationResponse,
)
from .store import Store

log = structlog.get_logger(__name__)

env = Env()

MAX_RETRIES = env.int("PYNIXD_BUILD_RETRIES", 3)
_PSI_PRESSURE_THRESHOLD = env.float("PYNIXD_PSI_THRESHOLD", 70.0)

# Build statuses that are worth retrying (possibly transient).
# PermanentFailure (3), InputRejected (4), OutputRejected (5),
# CachedFailure (7), DependencyFailed (12), NotDeterministic (14)
# are NOT retried — the build itself is broken.
_RETRYABLE_STATUSES: frozenset[int] = frozenset(
    {
        # BuildResultStatus.DEPENDENCY_FAILED,
        BuildResultStatus.TRANSIENT_FAILURE,
        BuildResultStatus.TIMED_OUT,
        BuildResultStatus.MISC_FAILURE,
        BuildResultStatus.LOG_LIMIT_EXCEEDED,
    }
)


@dataclass(frozen=True)
class CandidateStore:
    """A candidate store for scheduling a build, with all ranking metadata."""

    store_id: str
    score: int
    is_high_pressure: bool
    in_failed: bool
    pressure: float


class BuildReadiness(Enum):
    """Why a build cannot be scheduled yet."""

    BUILDING = auto()  # build task already running
    DONE = auto()  # already complete
    NO_STORE = auto()  # no compatible store
    WAITING_DAG = auto()  # missing required_paths in local store
    SCHEDULABLE = auto()  # ready to assign


class Scheduler:
    """Schedules builds across stores based on locality and DAG deps."""

    def __init__(
        self,
        build_queue: BuildQueue,
        stores: Mapping[str, Store],
        local_store: Store,
    ) -> None:
        self.queue = build_queue
        self.stores = stores
        self.local_store = local_store
        self.trigger_event = asyncio.Event()
        self.running = False

    def trigger(self) -> None:
        """Signal that a scheduling pass is needed."""
        self.trigger_event.set()

    async def start(self) -> None:
        """Start the scheduler loop."""
        self.running = True
        log.info("scheduler_started")
        while self.running:
            await self.trigger_event.wait()
            self.trigger_event.clear()
            await self.run_scheduling_pass()

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self.running = False
        self.trigger_event.set()
        builds = await self.queue.get_pending()
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

    def is_schedulable(self, build: QueuedBuild) -> bool:
        """A build is schedulable when all required_paths exist on local."""
        return self.local_store.has_all_paths(build.required_paths)

    def compute_ranking(self, build: QueuedBuild) -> list[CandidateStore]:
        """Rank stores by locality (common paths with required_paths).

        Filters out stores that don't support the build's platform,
        and lix stores for derivations that require nix features
        (dynamic drvs, CA floating, deferred, impure outputs).

        Stores where this build previously failed are deprioritized
        (sorted after non-failed stores at the same locality score).

        If the derivation signals build_local, the local_store is added
        as a candidate with maximum score (it already has all inputs).
        """
        needs_nix = build.request.derivation.requires_nix
        failed = set(build.failed_backends)

        candidates: list[CandidateStore] = []
        for s in self.stores.values():
            if not s.supports_system(build.platform):
                continue
            if needs_nix and s.is_lix:
                continue
            if not s.is_healthy:
                continue
            score = s.count_common_paths(build.required_paths)
            pressure = s.pressure or 0.0
            candidates.append(
                CandidateStore(
                    store_id=s.id,
                    score=score,
                    is_high_pressure=pressure >= _PSI_PRESSURE_THRESHOLD,
                    in_failed=s.id in failed,
                    pressure=pressure,
                )
            )

        # Add local_store as a candidate when derivation opts in
        if (
            build.request.derivation.build_local
            and self.local_store.supports_system(build.platform)
            and self.local_store.is_healthy
        ):
            candidates.append(
                CandidateStore(
                    store_id=self.local_store.id,
                    score=len(build.required_paths),
                    is_high_pressure=False,
                    in_failed=False,
                    pressure=0.0,
                )
            )

        # Sort: high pressure first, then locality, then not failed, then lower pressure
        candidates.sort(
            key=lambda c: (c.is_high_pressure, -c.score, c.in_failed, c.pressure)
        )
        return candidates

    # ── Build readiness classification ─────────────────────────────────

    def classify_build(self, build: QueuedBuild) -> BuildReadiness:
        """Classify why a build cannot be scheduled yet."""
        if build.is_building:
            return BuildReadiness.BUILDING
        if build.is_done:
            return BuildReadiness.DONE
        if not self.compute_ranking(build):
            return BuildReadiness.NO_STORE
        if not self.is_schedulable(build):
            return BuildReadiness.WAITING_DAG
        return BuildReadiness.SCHEDULABLE

    def resolve_store(self, store_id: str) -> Store:
        """Resolve a store_id to its Store instance."""
        if store_id == self.local_store.id:
            return self.local_store
        return self.stores[store_id]

    def effective_slots(
        self,
        store_id: str,
        assigned_this_pass: dict[str, int],
    ) -> int:
        if store_id == self.local_store.id:
            return self.local_store.available_slots - assigned_this_pass.get(
                self.local_store.id, 0
            )
        store = self.stores[store_id]
        return store.available_slots - assigned_this_pass.get(store_id, 0)

    def slots_exhausted(self, assigned_this_pass: dict[str, int]) -> bool:
        """True when all build slots are spoken for (in-use + assigned this pass)."""
        return self.total_available_slots() <= sum(assigned_this_pass.values())

    def total_available_slots(self) -> int:
        """Total free build slots across all healthy stores."""
        return sum(s.available_slots for s in self.stores.values() if s.is_healthy)

    async def run_scheduling_pass(self) -> None:
        """Run one scheduling pass."""
        builds = await self.queue.get_pending()
        if not builds:
            return

        # Skip the pass if every slot is occupied — nothing can be assigned.
        # Builds completing will trigger a new pass when slots free up.
        has_pending = any(b.is_pending for b in builds)
        if has_pending and self.slots_exhausted({}):
            return

        # Categorize builds for logging
        building: list[int] = []
        transferring: list[int] = []
        waiting_dag: list[int] = []
        waiting_slot: list[int] = []  # filled during assignment loop

        for b in builds:
            readiness = self.classify_build(b)
            match readiness:
                case BuildReadiness.BUILDING:
                    building.append(b.id)
                case BuildReadiness.DONE:
                    pass  # shouldn't be here
                case BuildReadiness.NO_STORE:
                    # Fail immediately — no compatible store
                    needs_nix = b.request.derivation.requires_nix
                    all_systems = sorted(
                        {s for s in self.stores.values() for s in s.supported_systems}
                    )
                    reason = (
                        f"No compatible store for {b.description} "
                        f"(platform={b.platform}, requires_nix={needs_nix}, "
                        f"stores: {', '.join(all_systems) or 'any (unconstrained)'})"
                    )
                    await self.queue.fail(b.id, reason)
                case BuildReadiness.WAITING_DAG:
                    waiting_dag.append(b.id)
                case BuildReadiness.SCHEDULABLE:
                    # will be waiting_slot if not assigned
                    pass

        slots = {s.id: s.available_slots for s in self.stores.values()}
        assigned_this_pass: dict[str, int] = {}

        for build in builds:
            readiness = self.classify_build(build)
            if readiness is not BuildReadiness.SCHEDULABLE:
                continue  # BUILDING, DONE, NO_STORE, or WAITING_DAG already handled

            # No slots left anywhere — stop trying to assign
            if self.slots_exhausted(assigned_this_pass):
                # Mark all remaining pending builds as waiting_slot
                for remaining in builds:
                    if remaining.is_pending and self.is_schedulable(remaining):
                        if (
                            remaining.id not in waiting_slot
                            and remaining.id != build.id
                        ):
                            waiting_slot.append(remaining.id)
                waiting_slot.append(build.id)
                break

            ranking = self.compute_ranking(build)
            if not ranking:
                continue  # already failed above

            top_score = ranking[0].score
            tied_top_with_slot = [
                c
                for c in ranking
                if c.score == top_score
                and self.effective_slots(c.store_id, assigned_this_pass) > 0
            ]

            if tied_top_with_slot:
                # Pick least-loaded among tied-top
                best = min(
                    tied_top_with_slot,
                    key=lambda c: self.resolve_store(c.store_id).in_flight,
                )
                store = self.resolve_store(best.store_id)
                log.debug(
                    "build_assigned_to_store",
                    build_id=build.id,
                    store_id=best.store_id,
                    score=best.score,
                    effective_slots=self.effective_slots(
                        best.store_id, assigned_this_pass
                    ),
                )
                self.start_build(build, store)
                assigned_this_pass[best.store_id] = (
                    assigned_this_pass.get(best.store_id, 0) + 1
                )
            else:
                waiting_slot.append(build.id)
                # Start proactive transfer to best store WITH a slot
                if not build.is_transferring:
                    for candidate in ranking:
                        if candidate.store_id == self.local_store.id:
                            continue
                        store = self.stores[candidate.store_id]
                        if (
                            self.effective_slots(candidate.store_id, assigned_this_pass)
                            > 0
                            and store.available_transfer_slots > 0
                        ):
                            self.start_transfer(build, store)
                            transferring.append(build.id)
                            break

        unhealthy = [s.id for s in self.stores.values() if not s.is_healthy]

        log.debug(
            "scheduling_pass_done",
            total_builds=len(builds),
            building=building,
            transferring=transferring,
            waiting_dag=waiting_dag,
            waiting_slot=waiting_slot,
            slots=slots,
        )
        if unhealthy:
            log.warning("stores_in_cooldown", unhealthy=unhealthy)
        pressure = {
            s.id: f"{s.pressure:.1f}"
            for s in self.stores.values()
            if s.pressure is not None
        }
        if pressure:
            log.debug("backend_pressure", pressure=pressure)
        memory = {
            s.id: f"{s.meminfo.available_mb}MB/{s.meminfo.total_mb}MB"
            for s in self.stores.values()
            if s.meminfo is not None
        }
        if memory:
            log.debug("backend_memory", memory=memory)

    # ── Build lifecycle ─────────────────────────────────────────────

    def start_build(self, build: QueuedBuild, store: Store) -> None:
        """Assign a build to a store and spawn its execution task."""
        build.started_at = time.monotonic()
        build.build_task = asyncio.create_task(
            self.execute_build(build, store),
        )

    def start_transfer(self, build: QueuedBuild, store: Store) -> None:
        """Start a proactive transfer to a store."""
        log.info(
            "proactive_transfer_started",
            build_id=build.id,
            store_id=store.id,
        )
        build.transfer_task = asyncio.create_task(
            self.do_proactive_transfer(build, store),
        )

    def should_retry(
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

    async def retry_build(self, build: QueuedBuild, store: Store) -> None:
        """Reset build state for retry on next scheduling pass."""
        # Capture the task we're stopping — a new scheduling pass may have
        # started a different transfer while stop_transfer() was awaiting.
        old_transfer_task = build.transfer_task
        await build.stop_transfer()
        build.reset_for_retry(store.id, old_transfer_task)
        log.info(
            "build_retrying",
            build_id=build.id,
            retry=build.retries,
            max_retries=MAX_RETRIES,
            failed_store_id=store.id,
        )

    async def execute_build(
        self,
        build: QueuedBuild,
        store: Store,
    ) -> None:
        """Execute a build on a store (runs as a spawned task).

        Acquires its own local_store connection so the build survives
        client proxy disconnect.

        After the build finishes, output paths are pulled to the local
        store. The scheduler is triggered immediately so the next build
        can start, but the result is not delivered to the client until
        the pull completes.
        """
        try:
            # Stop proactive transfer gracefully before acquiring connections
            await build.stop_transfer()

            is_local = store is self.local_store

            if not is_local:
                log.debug("build_sending_inputs", build_id=build.id, store_id=store.id)
                # Transfer required inputs
                await self.ensure_worker_has_inputs(build, store)

            log.debug("build_executing", build_id=build.id, store_id=store.id)
            assert isinstance(build.request, BuildDerivationRequest), (
                f"Build {build.id}: expected BuildDerivationRequest, "
                f"got {type(build.request).__name__}"
            )
            response = await self.execute_build_derivation(
                build,
                store,
            )

            if response.result.status not in (0, 1, 2):
                log.warning(
                    "unexpected_build_status",
                    status=response.result.status,
                    error_msg=response.result.error_msg,
                )

            if response.result.status != 0 and self.should_retry(
                build,
                response.result.status,
                store,
            ):
                if response.result.status in _RETRYABLE_STATUSES:
                    store.record_failure()
                await self.retry_build(build, store)
                return

            if response.result.status == 0:
                store.record_success()

            # Spawn pull as background task so build slot frees immediately.
            # The scheduler trigger fires below so the next build can start.
            # We await the pull task before completing so the client doesn't
            # see the result until outputs are in local_store.
            # Skip for local builds — outputs are already in local_store.
            pull_task: asyncio.Task | None = None
            if response.result.status == 0 and not is_local:
                built = response.result.built_outputs
                if built:
                    output_paths = []
                    for name, realisation in built.items():
                        p = realisation.get("outPath", "")
                        if p and not p.startswith("/nix/store/"):
                            p = f"/nix/store/{p}"
                        if p:
                            output_paths.append(p)
                        else:
                            log.warning("build_output_no_path", name=name)
                else:
                    output_paths = [
                        o.path for o in build.request.derivation.outputs if o.path
                    ]
                if output_paths:
                    pull_task = asyncio.create_task(
                        self.pull_paths(store, output_paths),
                        name=f"pull-{build.id}",
                    )

            # Trigger scheduler immediately so the next build can start
            self.trigger()

            # Wait for pull to complete before delivering result to client
            if pull_task is not None:
                await pull_task

            log.debug("build_completing", id=build.id)
            await self.queue.complete(build.id, response)
            log.debug("build_completed", id=build.id)
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
            if self.should_retry(build, None, store):
                await self.retry_build(build, store)
            else:
                log.exception(
                    "build_failed_no_retries",
                    build_id=build.id,
                    store_id=store.id,
                )
                await self.queue.fail(
                    build.id,
                    f"Build failed after {build.retries} retries "
                    f"(last: {store.id}): {e}",
                )
            self.trigger()
        except Exception as e:
            # Programming error — don't retry, don't blame the store
            log.exception("build_unexpected_error", id=build.id)
            await self.queue.fail(build.id, f"Internal error: {type(e).__name__}: {e}")
            self.trigger()

    async def execute_build_derivation(
        self,
        build: QueuedBuild,
        store: Store,
    ) -> BuildDerivationResponse:
        """Execute a BuildDerivation on a store."""
        assert isinstance(build.request, BuildDerivationRequest)
        response = await store.execute(
            build.request,
            client=build.client,
            suppress_last=True,
        )
        log.debug(
            "build_executed",
            build_id=build.id,
            status=response.result.status,
        )
        return response

    # ── Proactive transfer ──────────────────────────────────────────

    async def do_proactive_transfer(
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
            sorted_paths, infos = await self.compute_transfer_plan(
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
                        "proactive_transfer_cancelled",
                        build_id=build.id,
                        store_id=store.id,
                        transferred=transferred,
                        total_paths=len(sorted_paths),
                    )
                    return

                try:
                    await store.stream_paths_store_to_store(
                        src=self.local_store,
                        paths_with_info=[(path, infos[path])],
                    )
                    store.add_known_path(path)
                    transferred += 1
                except Exception:
                    log.debug(
                        "proactive_transfer_path_failed",
                        path=path,
                        store_id=store.id,
                    )

            log.info(
                "proactive_transfer_complete",
                build_id=build.id,
                store_id=store.id,
                transferred=transferred,
                total_paths=len(sorted_paths),
            )
            self.trigger()
        except Exception:
            log.exception(
                "proactive_transfer_failed",
                store_id=store.id,
                build_id=build.id,
            )
            self.trigger()

    # ── Path transfer helpers ───────────────────────────────────────

    async def ensure_closure(self, build: QueuedBuild) -> set[str]:
        """Return the cached runtime closure, computing it on first call.

        The closure only depends on required_paths and the local store's
        reference graph — both stable once the build is schedulable.
        """
        if build.closure is not None:
            return build.closure

        seeds = build.required_paths
        db = self.local_store.db
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
                    path_info = await self.local_store.query_path_info(path)
                    if path_info:
                        for ref in path_info.references:
                            if ref not in closure:
                                closure.add(ref)
                                queue.append(ref)
                except Exception:
                    pass

        log.debug(
            "closure_expanded",
            seed_count=len(seeds),
            closure_count=len(closure),
        )
        build.closure = closure
        return closure

    async def compute_transfer_plan(
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

        closure = await self.ensure_closure(build)

        # Diff against worker using known_paths (we control all transfers)
        missing = closure - store.known_paths
        if not missing:
            return [], {}

        log.info(
            "worker_missing_paths",
            store_id=store.id,
            missing_count=len(missing),
            total_closure=len(closure),
        )

        # Batch PathInfo for missing paths
        infos = await self.local_store.query_path_infos(missing)

        if not infos:
            log.debug(
                "no_transferable_paths",
            )
            return [], {}

        return self.topo_sort(infos), infos

    @staticmethod
    def topo_sort(infos: dict[str, PathInfo]) -> list[str]:
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

    async def ensure_worker_has_inputs(
        self,
        build: QueuedBuild,
        store: Store,
    ) -> None:
        """Send missing paths from local store to a worker."""
        sorted_paths, infos = await self.compute_transfer_plan(build, store)
        if not sorted_paths:
            return

        to_send = [(p, infos[p]) for p in sorted_paths]
        await store.stream_paths_store_to_store(
            src=self.local_store, paths_with_info=to_send
        )
        store.add_known_paths(set(sorted_paths))
        log.info(
            "missing_inputs_sent_to_worker",
            count=len(to_send),
            store_id=store.id,
        )

    async def pull_paths(
        self,
        store: Store,
        paths: list[str],
    ) -> None:
        """Pull store paths from store into pynixd's local store."""
        if not paths:
            return

        log.info("pulling_paths", count=len(paths), store_id=store.id)

        # Query PathInfo for all paths concurrently
        async def query_one(path: str) -> tuple[str, PathInfo] | None:
            try:
                path_info = await store.query_path_info(path)
                if path_info is None:
                    log.warning("path_not_valid_on_store", path=path, store_id=store.id)
                    return None
                path_info.path = path
                return (path, path_info)
            except Exception:
                log.exception("path_info_query_failed", path=path, store_id=store.id)
                return None

        results = await asyncio.gather(*[query_one(path) for path in paths])
        to_pull: list[tuple[str, PathInfo]] = []
        missing: list[str] = []
        for path, result in zip(paths, results):
            if result is not None:
                to_pull.append(result)
            else:
                missing.append(path)

        if missing:
            log.warning(
                "pull_paths_missing",
                missing_count=len(missing),
                total_paths=len(paths),
                store_id=store.id,
                missing_paths=missing[:10],
            )
            raise RuntimeError(
                f"Failed to query PathInfo for {len(missing)} output path(s) "
                f"from {store.id}: {missing[:3]}"
            )

        try:
            await self.local_store.stream_paths_store_to_store(
                src=store, paths_with_info=to_pull
            )
            for path, _ in to_pull:
                self.local_store.add_known_path(path)
                store.add_known_path(path)
            log.debug("pulled_paths_into_local_store", count=len(to_pull))
            self.trigger()
        except Exception:
            log.exception(
                "pull_paths_failed",
                count=len(to_pull),
                store_id=store.id,
            )
