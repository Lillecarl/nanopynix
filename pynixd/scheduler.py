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
from typing import TYPE_CHECKING

import structlog

from .build_queue import BuildQueue, QueuedBuild
from .connection import ClientConn
from .derived_path import DerivedPath
from .drv_parser import read_drv_file, to_basic_derivation
from .exceptions import BackendError, InfrastructureError
from .operations.base import BasicDerivation, BuildMode, KeyedBuildResult
from .operations.build_derivation import (
    BuildDerivationRequest,
    BuildDerivationResponse,
)
from .operations.build_paths import BuildPathsWithResultsResponse
from .operations.ca_derivations import RegisterDrvOutputRequest
from .operations.query_derivation_outputs_batch import (
    QueryDerivationOutputsBatchRequest,
)
from .operations.query_missing import QueryMissingRequest
from .operations.query_valid_paths import QueryValidPathsRequest

from .store import Store
from .store_path import StorePath

if TYPE_CHECKING:
    from .drv_parser import ParsedDerivation

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


# Duration threshold for "tiny" builds that can be fast-tracked to the local store (2.5s)
TINY_BUILD_THRESHOLD_MS = 2500


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
        """Decompose DerivedPath set into individual builds and execute them.

        Handles: substitution, .drv parsing, DAG linking, enqueueing.
        Returns a BuildPathsWithResultsResponse — callers that don't need
        per-key results (BuildPaths) can just check for failures.
        """
        _req_id, sched_req = await self.queue.create_request(
            derived_paths, build_mode, client
        )

        # For nested DerivedPaths (e.g., a.drv^out^out), QueryMissing
        # doesn't understand them. Extract the outermost .drv path for
        # the initial build phase — the nested chain will be handled by
        # the trampoline.
        flat_derived_paths: set[DerivedPath] = set()
        for dp in derived_paths:
            if isinstance(dp, DerivedPath) and dp.is_nested:
                outer_dp = DerivedPath(dp.drv_path)
                flat_derived_paths.add(outer_dp)
            else:
                flat_derived_paths.add(dp)

        missing_resp = await self.local_store.execute(
            QueryMissingRequest(derived_paths=flat_derived_paths)
        )

        if missing_resp.will_substitute:
            log.info("substituting_paths", count=len(missing_resp.will_substitute))
            async with self.local_store.transfer_conn() as conn:
                valid = await conn.call(
                    QueryValidPathsRequest(
                        paths=missing_resp.will_substitute,
                        substitute=1,
                    )
                )
                self.local_store.tracker.add_known_paths(valid.paths)

        drv_to_derived: dict[str, DerivedPath] = {}
        for dp in derived_paths:
            if isinstance(dp, DerivedPath):
                drv_to_derived.setdefault(dp.drv_path, dp)

        parsed_cache: dict[StorePath, ParsedDerivation] = {}
        all_planned_outputs: set[StorePath] = set()
        all_input_drvs: set[StorePath] = set()

        # Collect all derivations that need to be built, including
        # those referenced via dynamic_input_drvs.
        to_build: set[StorePath] = set()
        for sp in missing_resp.will_build | missing_resp.unknown:
            to_build.add(StorePath(sp))

        # Expand to_build with dynamic_input_drvs targets.
        # A wrapper's dynamic deps (producingDrv) may not appear in
        # will_build because the .drv file is a valid path but its
        # outputs haven't been built yet.
        queue = list(to_build)
        visited: set[StorePath] = set()
        while queue:
            sp = queue.pop(0)
            if sp in visited:
                continue
            visited.add(sp)
            dp = drv_to_derived.get(str(sp), DerivedPath(sp))
            try:
                parsed = dp.to_derivation(self.local_store.store_path)
            except FileNotFoundError:
                log.warning("drv_read_failed", drv_path=dp.drv_path)
                continue

            parsed_cache[StorePath(dp.drv_path)] = parsed
            for p in parsed.output_paths().values():
                if p != StorePath(""):
                    all_planned_outputs.add(p)
            all_input_drvs.update(parsed.input_drvs.keys())

            # Add dynamic_input_drvs targets to the build queue.
            # These are derivations whose outputs are needed but may
            # not be in will_build (their .drv files are valid paths
            # but their outputs aren't built yet).
            for dyn_drv_path in parsed.dynamic_input_drvs:
                if dyn_drv_path not in to_build:
                    # Check if this dynamic dep's outputs are already available
                    try:
                        dyn_parsed = read_drv_file(
                            self.local_store.store_path, dyn_drv_path
                        )
                    except FileNotFoundError:
                        continue
                    dyn_outputs = dyn_parsed.output_paths()
                    has_unbuilt = any(
                        p == StorePath("") or not self.local_store.tracker.has_path(p)
                        for p in dyn_outputs.values()
                    )
                    if has_unbuilt:
                        to_build.add(dyn_drv_path)
                        queue.append(dyn_drv_path)
                        parsed_cache[dyn_drv_path] = dyn_parsed
                        all_input_drvs.update(dyn_parsed.input_drvs.keys())

        output_cache = None
        if all_input_drvs:
            resp = await self.local_store.execute(
                QueryDerivationOutputsBatchRequest(drv_paths=all_input_drvs)
            )
            output_cache = resp.outputs if resp.outputs else {}

        resolved: list[tuple[DerivedPath, set[str], BuildDerivationRequest]] = []
        all_input_srcs: set[StorePath] = set()

        for sp in to_build:
            dp = drv_to_derived.get(str(sp), DerivedPath(sp))
            drv_path = StorePath(dp.drv_path)
            parsed = parsed_cache.get(drv_path)
            if parsed is None:
                continue

            basic = to_basic_derivation(
                parsed, self.local_store.store_path, output_cache=output_cache
            )
            drv_request = BuildDerivationRequest(
                drv_path=drv_path,
                derivation=basic,
                build_mode=build_mode,
            )
            resolved.append((dp, dp.output_names, drv_request))
            all_input_srcs.update(basic.input_srcs)

        unknown = all_input_srcs - self.local_store.tracker.known_paths
        if unknown:
            valid_resp = await self.local_store.execute(
                QueryValidPathsRequest(paths=unknown)
            )
            self.local_store.tracker.add_known_paths(
                valid_resp.paths, update_regtime=False
            )

        drv_to_build_id: dict[str, int] = {}

        for dp, output_names, drv_request in resolved:
            if drv_request.drv_path in parsed_cache:
                drv_request.derivation.is_dynamic = parsed_cache[
                    drv_request.drv_path
                ].is_dynamic
            else:
                try:
                    parsed = read_drv_file(
                        self.local_store.store_path, drv_request.drv_path
                    )
                    drv_request.derivation.is_dynamic = parsed.is_dynamic
                except FileNotFoundError:
                    pass
                except Exception:
                    pass

            drv_path_str = str(drv_request.drv_path)
            required_paths: set[StorePath] = set()
            for inp in drv_request.derivation.input_srcs:
                required_paths.add(
                    StorePath(inp, extrainfo=f"input_src of {drv_path_str}")
                )
            required_paths.add(
                StorePath(drv_request.drv_path, extrainfo=f"drv_path of {drv_path_str}")
            )
            build_id, _future = await self.build_derivation(
                drv_request,
                client,
                required_paths,
                platform=drv_request.derivation.platform,
                scheduler_request_id=sched_req.id,
                derived_paths_for_request={dp},
            )
            drv_to_build_id[drv_path_str] = build_id
            log.info(
                "build_derivation_enqueued",
                build_id=build_id,
                drv_path=drv_request.drv_path,
                scheduler_request_id=sched_req.id,
            )

        for dp, output_names, drv_request in resolved:
            drv_path_str = str(drv_request.drv_path)
            parsed = parsed_cache.get(drv_request.drv_path)
            if parsed is None:
                continue

            depends_on: set[int] = set()
            for input_drv in parsed.input_drvs:
                dep_id = drv_to_build_id.get(str(input_drv))
                if dep_id is not None and dep_id != drv_to_build_id.get(drv_path_str):
                    depends_on.add(dep_id)

            # Dynamic input derivations: add depends_on edges to the
            # outer build. When the trampoline fires and creates inner
            # builds, _on_build_complete will add edges to those too.
            for dyn_drv in parsed.dynamic_input_drvs:
                dep_id = drv_to_build_id.get(str(dyn_drv))
                if dep_id is not None and dep_id != drv_to_build_id.get(drv_path_str):
                    depends_on.add(dep_id)

            # Store dynamic_input_drvs on the QueuedBuild so the
            # trampoline can use it later.
            build_id = drv_to_build_id.get(drv_path_str)
            if build_id is not None and parsed.dynamic_input_drvs:
                build = self.queue.by_id.get(build_id)
                if build is not None:
                    build.dynamic_input_drvs = parsed.dynamic_input_drvs

            if depends_on:
                build_id = drv_to_build_id[drv_path_str]
                await self.queue.set_depends_on(build_id, depends_on)

        if not resolved:
            result_map = await sched_req.future
        else:
            result_map = await sched_req.future

        keyed_results: list[KeyedBuildResult] = []
        for dp in derived_paths:
            if isinstance(dp, DerivedPath):
                br = result_map.get(dp)
                if br is not None:
                    keyed_results.append(KeyedBuildResult(derived_path=dp, result=br))

        return BuildPathsWithResultsResponse(results=keyed_results)

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
            build_features = build.request.derivation.required_system_features
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
                    build.build_task = asyncio.create_task(
                        self.execute_build(build, self.local_store)
                    )
                    building.append(build.id)
                    continue

            # 2. Standard remote backend assignment
            ranked = self.rank_stores(build)

            # If NO store will ever support this platform/features, fail it statelessly
            if not ranked and not any(
                s.supports_derivation(build.platform, build_features)
                for s in self.stores.values()
            ):
                reasons = self._incompatibility_reasons(build.platform, build_features)
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
                    from .stderr import StderrNext

                    for line in error_msg.split("\n"):
                        client.queue.put_nowait(StderrNext(text=f"pynixd: {line}\n"))
                if build.scheduler_request_id is not None:
                    await self._on_build_complete_failed(build, error_msg)
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
                        from .stderr import StderrNext

                        for line in error_msg.split("\n"):
                            client.queue.put_nowait(
                                StderrNext(text=f"pynixd: {line}\n")
                            )
                    if build.scheduler_request_id is not None:
                        await self._on_build_complete_failed(build, error_msg)
                    continue
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
                missing = build.required_paths - rs.store.tracker.known_paths
                if missing and self.local_store.tracker.has_all_paths(missing):
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
            waiting_deps=len(waiting_deps),
            waiting_slot=len(waiting_slot),
            slots={s.id: s.available_slots for s in self.stores.values()},
            cpu_util={
                s.id: f"{s.cpu_util.utilization:.1f}%" if s.cpu_util else None
                for s in self.stores.values()
            },
        )

    def _incompatibility_reasons(
        self, platform: str, features: set[str] | None
    ) -> list[str]:
        """Build per-store incompatibility explanations for error reporting."""
        reasons: list[str] = []
        fm = self.local_store._feature_matrix
        if fm is not None and platform not in fm:
            reasons.append(f"local: system {platform} not in feature_matrix")
        elif fm is not None and features:
            local_feats = fm.get(platform, set())
            missing = features - local_feats
            if missing:
                reasons.append(
                    f"local: missing features {', '.join(sorted(missing))} for {platform}"
                )
        elif fm is None:
            reasons.append("local: no feature_matrix (not probed)")
        else:
            reasons.append("local: compatible")

        all_stores = list(self.stores.values())
        for store in all_stores:
            sfm = store._feature_matrix
            if sfm is not None and platform not in sfm:
                reasons.append(f"{store.id}: system {platform} not in feature_matrix")
            elif sfm is not None and features:
                store_feats = sfm.get(platform, set())
                missing = features - store_feats
                if missing:
                    reasons.append(
                        f"{store.id}: missing features {', '.join(sorted(missing))} for {platform}"
                    )
                else:
                    reasons.append(
                        f"{store.id}: compatible but excluded (unhealthy/saturated/failed)"
                    )
            elif sfm is None:
                reasons.append(f"{store.id}: no feature_matrix (not probed)")
            else:
                reasons.append(
                    f"{store.id}: compatible but excluded (unhealthy/saturated/failed)"
                )

        return reasons

    def rank_stores(self, build: QueuedBuild) -> RankedStores:
        """Rank stores for a build by path overlap, tiebreak by available slots."""
        build_features = build.request.derivation.required_system_features
        stores = []
        for store_id, store in self.stores.items():
            if not store.is_healthy:
                continue
            if not store.supports_derivation(build.platform, build_features):
                continue
            if store_id in build.failed_backends:
                continue
            if store.cpu_util is not None and store.cpu_util.utilization > 99.0:
                continue

            score = store.tracker.count_common_paths(build.required_paths)
            stores.append(RankedStore(store_id, score, store.available_slots, store))

        return RankedStores(stores).sort()

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
                    await self._register_dep_realisations(build, store)
                    await self._resolve_deferred_derivation(build, store)
                    await self._resolve_dynamic_derivation(build, store)

                # 1. Ensure all inputs are present on the builder
                missing = build.required_paths - store.tracker.known_paths
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
                    await Store.stream_paths_store_to_store(
                        store, self.local_store, all_output_paths
                    )
                    log.debug(
                        "pulled_paths_into_local_store",
                        count=len(all_output_paths),
                        store_id=store.id,
                    )

                    # Register CA realisations after outputs are in local store
                    if resp.result.built_outputs:
                        for (
                            drv_output_str,
                            realisation,
                        ) in resp.result.built_outputs.items():
                            try:
                                reg_req = RegisterDrvOutputRequest(
                                    realisation=realisation
                                )
                                await self.local_store.execute(
                                    reg_req, suppress_last=True
                                )
                            except Exception:
                                log.warning(
                                    "register_drv_output_failed",
                                    drv_output=drv_output_str,
                                    exc_info=True,
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
            if build.scheduler_request_id is not None:
                await self._on_build_complete_failed(build, "Internal scheduler error")
            self.trigger()

        # Release semaphore FIRST, then complete and trigger
        # This allows new builds to start while we're finalizing
        if build_resp is not None:
            await self.queue.complete(build.id, build_resp)
            if build.scheduler_request_id is not None:
                await self._on_build_complete(build, build_resp)
            self.trigger()

    async def _register_dep_realisations(
        self, build: QueuedBuild, store: Store
    ) -> None:
        """Register CA realisations from completed dependency builds on the
        target builder store so it can resolve deferred output paths.

        This is essential for building non-CA derivations that depend on CA
        derivations: the builder daemon needs the realisation registered so
        it can resolve the deferred output's $out path.
        """
        for dep_id in build.depends_on:
            dep_build = self.queue.by_id.get(dep_id)
            if dep_build is None or not dep_build.ca_realisations:
                continue

            if store is self.local_store:
                # Realisations already registered on local store during
                # the dependency build's completion
                continue

            for realisation in dep_build.ca_realisations:
                try:
                    reg_req = RegisterDrvOutputRequest(realisation=realisation)
                    log.debug(
                        "registering_dep_realisation_on_builder",
                        build_id=build.id,
                        dep_build_id=dep_id,
                        store_id=store.id,
                        realisation=realisation,
                    )
                    await store.call(reg_req, suppress_last=True)
                    log.debug(
                        "registered_dep_realisation_on_builder",
                        build_id=build.id,
                        dep_build_id=dep_id,
                        store_id=store.id,
                    )
                except Exception as exc:
                    log.warning(
                        "register_dep_realisation_failed",
                        build_id=build.id,
                        dep_build_id=dep_id,
                        store_id=store.id,
                        exc_info=True,
                        error=str(exc),
                    )

    async def _resolve_deferred_derivation(
        self, build: QueuedBuild, store: Store
    ) -> None:
        """Resolve a deferred derivation before building.

        Only needed for builds decomposed from BuildPaths, where pynixd
        creates BuildDerivation from .drv files. Client-sent BuildDerivation
        via --builders should already be resolved by the local Nix daemon.

        The BuildDerivation wire protocol sends BasicDerivation (no inputDrvs).
        The daemon cannot resolve deferred derivations because it reads the
        .drv from disk, and for a deferred .drv queryPartialDerivationOutputMap
        returns None. We resolve the derivation ourselves: compute placeholder
        rewrites, derive output paths, then add the resolved .drv to both the
        local and builder stores via AddToStore so the daemon reads matching
        content from the new resolved .drv path.
        """
        from .derivation_resolution import (
            resolve_derivation,
            _unparse_basic_derivation,
            _nix_drv_name,
        )
        from .drv_parser import read_drv_file
        from .operations.add_to_store import AddToStoreRequest
        from .operations.base import OutputKind

        if not any(
            o.kind == OutputKind.DEFERRED
            for o in build.request.derivation.outputs.values()
        ):
            return

        drv_path = build.request.drv_path

        try:
            parsed = read_drv_file(self.local_store.store_path, drv_path)
        except FileNotFoundError:
            log.warning(
                "resolve_deferred_drv_not_found",
                build_id=build.id,
                drv_path=drv_path,
            )
            return

        if not parsed.input_drvs:
            return

        resolved_output_paths: dict[str, StorePath] = {}
        for dep_id in build.depends_on:
            dep_build = self.queue.by_id.get(dep_id)
            if dep_build is None or not dep_build.ca_realisations:
                continue
            for realisation in dep_build.ca_realisations:
                out_path = realisation.get("outPath", "")
                output_name = realisation.get("id", "").rsplit("!", 1)[-1] or "out"
                if out_path:
                    resolved_output_paths[output_name] = StorePath(
                        out_path
                    ).with_store_prefix()

        if not resolved_output_paths:
            log.warning(
                "resolve_deferred_no_output_paths",
                build_id=build.id,
                drv_path=drv_path,
            )
            return

        try:
            resolved = resolve_derivation(parsed, drv_path, resolved_output_paths)
        except Exception:
            log.exception(
                "resolve_derivation_failed",
                build_id=build.id,
                drv_path=drv_path,
            )
            return

        resolved_aterm = _unparse_basic_derivation(resolved, mask_outputs=False)

        drv_name = _nix_drv_name(drv_path)
        name_for_add = drv_name + ".drv"

        async def provide_resolved_drv(writer):
            fw = writer.framed()
            data = resolved_aterm.encode("utf-8")
            fw.write(data)
            await fw.finalize()

        resolved_drv_path: StorePath | None = None
        for target_store in {self.local_store, store}:
            add_req = AddToStoreRequest(
                path_name=name_for_add,
                cam="text:sha256",
                references=resolved.input_srcs,
                repair=0,
                async_provider=provide_resolved_drv,
            )
            try:
                resp = await add_req.execute(target_store, suppress_last=True)
                if resp.info is not None:
                    target_store.tracker.add_known_path(resp.info.path)
                    target_store.add_path_info(resp.info)
                    if resolved_drv_path is None:
                        resolved_drv_path = resp.info.path
                    log.debug(
                        "resolved_drv_added_to_store",
                        build_id=build.id,
                        store_id=target_store.id,
                        resolved_drv_path=resp.info.path,
                    )
            except Exception:
                log.warning(
                    "resolved_drv_add_to_store_failed",
                    build_id=build.id,
                    store_id=target_store.id,
                    exc_info=True,
                )

        if resolved_drv_path is None:
            log.error("resolve_deferred_add_failed", build_id=build.id)
            return

        build.request.drv_path = resolved_drv_path
        build.request.derivation = resolved

        build.required_paths.add(resolved_drv_path)
        for inp in resolved.input_srcs:
            build.required_paths.add(StorePath(inp))
        for name, o in resolved.outputs.items():
            if o.path:
                build.required_paths.add(StorePath(o.path))

        log.info(
            "resolved_deferred_derivation",
            build_id=build.id,
            drv_path=drv_path,
            resolved_drv_path=resolved_drv_path,
            output_paths={n: o.path for n, o in resolved.outputs.items()},
        )

    async def _resolve_dynamic_derivation(
        self, build: QueuedBuild, store: Store
    ) -> None:
        """Resolve a dynamic (DrvWithVersion) wrapper derivation before building.

        Wrapper derivations have dynamic_input_drvs referencing dynamic
        outputs (drv^out^out). Their env/args contain DownstreamPlaceholder
        strings that must be rewritten to actual store paths.

        The resolution flow mirrors _resolve_deferred_derivation but uses
        resolve_dynamic_derivation() which handles nested placeholders.
        """
        from .derivation_resolution import (
            resolve_dynamic_derivation,
            _unparse_basic_derivation,
            _nix_drv_name,
        )
        from .drv_parser import read_drv_file
        from .operations.add_to_store import AddToStoreRequest

        if not build.dynamic_input_drvs:
            return

        drv_path = build.request.drv_path

        try:
            parsed = read_drv_file(self.local_store.store_path, drv_path)
        except FileNotFoundError:
            log.warning(
                "resolve_dynamic_drv_not_found",
                build_id=build.id,
                drv_path=drv_path,
            )
            return

        # Build the dynamic_output_paths map from dependency builds:
        # {(outer_drv_path, outer_output, inner_output): actual_store_path}
        dynamic_output_paths: dict[tuple[StorePath, str, str], StorePath] = {}

        # First, collect all dep build realisations, keyed by drv_path
        dep_realisations: dict[StorePath, dict[str, StorePath]] = {}
        for dep_id in build.depends_on:
            dep_build = self.queue.by_id.get(dep_id)
            if dep_build is None or not dep_build.ca_realisations:
                continue
            dep_drv_path = StorePath(dep_build.request.drv_path)
            for realisation in dep_build.ca_realisations:
                out_path = realisation.get("outPath", "")
                output_name = realisation.get("id", "").rsplit("!", 1)[-1] or "out"
                if out_path:
                    dep_realisations.setdefault(dep_drv_path, {})[output_name] = (
                        StorePath(out_path).with_store_prefix()
                    )

        for dyn_drv_path, output_deps in build.dynamic_input_drvs.items():
            # Level 1: outer drv's outputs (e.g., producingDrv^out = .drv path)
            outer_outputs = dep_realisations.get(dyn_drv_path, {})

            for outer_output, inner_outputs in output_deps.items():
                level1_path = outer_outputs.get(outer_output)
                if level1_path is None:
                    log.warning(
                        "resolve_dynamic_no_outer_output",
                        build_id=build.id,
                        drv_path=dyn_drv_path,
                        output=outer_output,
                    )
                    continue

                # The level-1 output is a .drv — find its build's realisations
                for inner_output_name in inner_outputs:
                    if level1_path.is_derivation():
                        inner_outputs_map = dep_realisations.get(level1_path, {})
                        actual_path = inner_outputs_map.get(inner_output_name)
                        if actual_path is not None:
                            dynamic_output_paths[
                                (dyn_drv_path, outer_output, inner_output_name)
                            ] = actual_path
                    else:
                        dynamic_output_paths[
                            (dyn_drv_path, outer_output, inner_output_name)
                        ] = level1_path

        if not dynamic_output_paths:
            log.warning(
                "resolve_dynamic_no_output_paths",
                build_id=build.id,
                drv_path=drv_path,
            )
            return

        try:
            resolved = resolve_dynamic_derivation(
                parsed, drv_path, dynamic_output_paths
            )
        except Exception:
            log.exception(
                "resolve_dynamic_derivation_failed",
                build_id=build.id,
                drv_path=drv_path,
            )
            return

        resolved_aterm = _unparse_basic_derivation(resolved, mask_outputs=False)

        log.debug(
            "resolve_dynamic_derivation_debug",
            build_id=build.id,
            drv_path=drv_path,
            dynamic_output_paths={
                str(k): str(v) for k, v in dynamic_output_paths.items()
            },
            resolved_outputs={n: o.path for n, o in resolved.outputs.items()},
            resolved_input_srcs=[str(p) for p in resolved.input_srcs],
            resolved_aterm_len=len(resolved_aterm),
        )

        drv_name = _nix_drv_name(drv_path)
        name_for_add = drv_name + ".drv"

        async def provide_resolved_drv(writer):
            fw = writer.framed()
            data = resolved_aterm.encode("utf-8")
            fw.write(data)
            await fw.finalize()

        resolved_drv_path: StorePath | None = None
        for target_store in {self.local_store, store}:
            add_req = AddToStoreRequest(
                path_name=name_for_add,
                cam="text:sha256",
                references=resolved.input_srcs,
                repair=0,
                async_provider=provide_resolved_drv,
            )
            try:
                resp = await add_req.execute(target_store, suppress_last=True)
                if resp.info is not None:
                    target_store.tracker.add_known_path(resp.info.path)
                    target_store.add_path_info(resp.info)
                    if resolved_drv_path is None:
                        resolved_drv_path = resp.info.path
                    log.debug(
                        "resolved_dynamic_drv_added_to_store",
                        build_id=build.id,
                        store_id=target_store.id,
                        resolved_drv_path=resp.info.path,
                    )
            except Exception:
                log.warning(
                    "resolved_dynamic_drv_add_to_store_failed",
                    build_id=build.id,
                    store_id=target_store.id,
                    exc_info=True,
                )

        if resolved_drv_path is None:
            log.error("resolve_dynamic_add_failed", build_id=build.id)
            return

        build.request.drv_path = resolved_drv_path
        build.request.derivation = resolved

        build.required_paths.add(resolved_drv_path)
        for inp in resolved.input_srcs:
            build.required_paths.add(StorePath(inp))
        for name, o in resolved.outputs.items():
            if o.path:
                build.required_paths.add(StorePath(o.path))

        log.info(
            "resolved_dynamic_derivation",
            build_id=build.id,
            drv_path=drv_path,
            resolved_drv_path=resolved_drv_path,
            output_paths={n: o.path for n, o in resolved.outputs.items()},
        )

    async def _on_build_complete(
        self,
        build: QueuedBuild,
        build_resp: BuildDerivationResponse,
    ) -> None:
        """Handle build completion within a SchedulerBuildRequest.

        For non-dynamic builds, records the result directly and checks if
        the request is complete. For dynamic builds (has_dynamic_outputs),
        detects .drv outputs and enqueues inner builds (trampoline).
        """

        if build.scheduler_request_id is None:
            return
        sched_req = self.queue.requests.get(build.scheduler_request_id)
        if sched_req is None:
            return

        parent_dps = sched_req.build_to_derived.get(build.id, set())

        derivation = build.request.derivation
        is_dynamic = derivation.has_dynamic_outputs
        has_nested_dp = any(dp.is_nested for dp in parent_dps)

        drv_outputs = build_resp.result.built_outputs
        trampolined_dps: set[DerivedPath] = set()

        has_drv_output = False
        has_dynamic_dependent = False
        if is_dynamic and build_resp.result.status == 0 and drv_outputs:
            for _drv_output_str, realisation in drv_outputs.items():
                out_path = realisation.get("outPath", "")
                if out_path:
                    out_sp = StorePath(out_path).with_store_prefix()
                    if out_sp.is_derivation():
                        has_drv_output = True
                        break

        # Check if any queued build has dynamic_input_drvs referencing
        # this build's drv path — those builds need the inner .drv's
        # outputs resolved, so we must trampoline.
        if has_drv_output and not has_nested_dp:
            build_drv_path = StorePath(build.request.drv_path)
            for _bid, other_build in self.queue.by_id.items():
                if other_build.is_done:
                    continue
                if not other_build.dynamic_input_drvs:
                    continue
                if build_drv_path in other_build.dynamic_input_drvs:
                    has_dynamic_dependent = True
                    break

        if (
            is_dynamic
            and (has_nested_dp or has_dynamic_dependent)
            and build_resp.result.status == 0
            and drv_outputs
        ):
            for _drv_output_str, realisation in drv_outputs.items():
                out_path = realisation.get("outPath", "")
                output_name = realisation.get("id", "").rsplit("!", 1)[-1] or "out"
                if not out_path:
                    continue

                out_sp = StorePath(out_path).with_store_prefix()
                if not out_sp.is_derivation():
                    continue

                log.info(
                    "trampoline_detected",
                    build_id=build.id,
                    output_name=output_name,
                    inner_drv_path=out_sp,
                )

                try:
                    inner_parsed = read_drv_file(self.local_store.store_path, out_sp)
                except FileNotFoundError:
                    log.warning(
                        "trampoline_drv_not_found",
                        build_id=build.id,
                        inner_drv_path=out_sp,
                    )
                    continue
                except Exception:
                    log.exception(
                        "trampoline_drv_parse_failed",
                        build_id=build.id,
                        inner_drv_path=out_sp,
                    )
                    continue

                inner_basic = to_basic_derivation(
                    inner_parsed, self.local_store.store_path
                )

                unknown_srcs = (
                    inner_basic.input_srcs - self.local_store.tracker.known_paths
                )
                if unknown_srcs:
                    try:
                        valid_resp = await self.local_store.execute(
                            QueryValidPathsRequest(paths=unknown_srcs)
                        )
                        self.local_store.tracker.add_known_paths(
                            valid_resp.paths, update_regtime=False
                        )
                    except Exception:
                        log.warning(
                            "trampoline_unknown_srcs_check_failed",
                            build_id=build.id,
                            inner_drv_path=out_sp,
                        )

                inner_req = BuildDerivationRequest(
                    drv_path=out_sp,
                    derivation=inner_basic,
                    build_mode=sched_req.build_mode,
                )

                required_paths: set[StorePath] = set()
                for inp in inner_basic.input_srcs:
                    required_paths.add(StorePath(inp))
                required_paths.add(out_sp)

                inner_build_id, _inner_future = await self.build_derivation(
                    inner_req,
                    sched_req.client,
                    required_paths,
                    platform=inner_basic.platform,
                    scheduler_request_id=sched_req.id,
                    derived_paths_for_request=parent_dps,
                )

                log.info(
                    "trampoline_build_enqueued",
                    parent_build_id=build.id,
                    inner_build_id=inner_build_id,
                    inner_drv_path=out_sp,
                    scheduler_request_id=sched_req.id,
                    original_derived_paths=[str(dp) for dp in parent_dps],
                )

                # Link dependent builds to the inner build via DAG.
                # Builds with dynamic_input_drvs referencing the outer
                # build's drv need to depend on the inner build too,
                # and need its output paths in required_paths.
                self._link_dynamic_deps(build, inner_build_id, inner_basic)

                trampolined_dps.update(parent_dps)

        # Record results for DerivedPaths that are NOT being trampolined.
        # Trampolined DerivedPaths will get their result from the inner build.
        non_trampolined_dps = parent_dps - trampolined_dps
        for dp in non_trampolined_dps:
            sched_req.results[dp] = build_resp.result

        sched_req.build_completed(build.id)

        if sched_req.resolve_if_done():
            log.info(
                "scheduler_request_resolved",
                request_id=sched_req.id,
                results=len(sched_req.results),
            )

        self.trigger()

    async def _on_build_complete_failed(
        self,
        build: QueuedBuild,
        error_msg: str,
    ) -> None:
        """Handle build failure within a SchedulerBuildRequest."""
        from .operations.base import BuildResult, BuildResultStatus

        if build.scheduler_request_id is None:
            return
        sched_req = self.queue.requests.get(build.scheduler_request_id)
        if sched_req is None:
            return

        parent_dps = sched_req.build_to_derived.get(build.id, set())
        failed_result = BuildResult(
            status=BuildResultStatus.MISC_FAILURE, error_msg=error_msg
        )
        for dp in parent_dps:
            sched_req.results[dp] = failed_result
        sched_req.build_completed(build.id)

        if sched_req.resolve_if_done():
            log.info(
                "scheduler_request_resolved_with_failure",
                request_id=sched_req.id,
                error_msg=error_msg,
            )

        self.trigger()

    def _link_dynamic_deps(
        self,
        outer_build: QueuedBuild,
        inner_build_id: int,
        inner_derivation: BasicDerivation,
    ) -> None:
        """After trampoline enqueues an inner build, add DAG edges from
        dependent builds to the inner build, and add the inner build's
        output paths to their required_paths.

        This ensures that builds with dynamic_input_drvs referencing
        the outer build wait for the inner build and have its outputs
        available when they execute.
        """
        outer_drv_path = StorePath(outer_build.request.drv_path)
        inner_outputs = inner_derivation.output_paths()
        inner_output_paths: set[StorePath] = {
            p for p in inner_outputs.values() if p != StorePath("")
        }

        for _bid, other_build in self.queue.by_id.items():
            if other_build.is_done:
                continue
            if not other_build.dynamic_input_drvs:
                continue

            # Does this build depend on the outer build's drv path
            # via dynamic_input_drvs?
            if outer_drv_path not in other_build.dynamic_input_drvs:
                continue

            # Add depends_on edge to the inner build
            if inner_build_id not in other_build.depends_on:
                other_build.depends_on.add(inner_build_id)
                log.info(
                    "dynamic_dep_linked",
                    dependent_build_id=other_build.id,
                    inner_build_id=inner_build_id,
                    outer_build_id=outer_build.id,
                )

            # Add inner build's output paths to required_paths
            for p in inner_output_paths:
                if p not in other_build.required_paths:
                    other_build.required_paths.add(p)
                    log.debug(
                        "dynamic_dep_required_path_added",
                        dependent_build_id=other_build.id,
                        path=p,
                    )

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
