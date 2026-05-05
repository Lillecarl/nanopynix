"""Post-build trampoline and lifecycle handling for dynamic derivations.

When a dynamic derivation produces a .drv file as output, that inner
derivation must be built before the original request can be satisfied.
This module handles the "trampoline" — detecting .drv outputs,
enqueuing inner builds, and rewiring DAG dependencies.

Also handles recording results (or failures) back to the
SchedulerBuildRequest that owns each build.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from .drv_parser import read_drv_file, to_basic_derivation
from .operations.base import BuildResult, BuildResultStatus, UnkeyedValidPathInfo
from .operations.build_derivation import BuildDerivationRequest
from .operations.query_valid_paths import QueryValidPathsRequest
from .store_path import DrvOutput, StorePath

if TYPE_CHECKING:
    from .build_queue import QueuedBuild, SchedulerBuildRequest
    from .derived_path import DerivedPath
    from .operations.base import BasicDerivation
    from .operations.build_derivation import BuildDerivationResponse
    from .scheduler import DerivationReader, Scheduler
    from .types.aliases import StorePathSet
    from .types.ca import Realisation
    from .types.ids import BuildId

log = structlog.get_logger(__name__)


class Trampoline:
    """Handles post-build trampolining and SchedulerBuildRequest lifecycle.

    After a dynamic derivation build completes, this class decides whether
    to fire the trampoline (enqueue inner builds for .drv outputs) or
    record the result directly.
    """

    def __init__(
        self,
        scheduler: Scheduler,
        read_drv_fn: DerivationReader | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.local_store = scheduler.local_store
        self.queue = scheduler.queue
        self.read_drv_fn = read_drv_fn or read_drv_file

    async def on_build_complete(
        self,
        build: QueuedBuild,
        build_resp: BuildDerivationResponse,
    ) -> None:
        """Handle build completion within a SchedulerBuildRequest."""
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
        build_succeeded = build_resp.result.status == 0

        should_trampoline = build_succeeded and self._should_trampoline(
            build,
            is_dynamic,
            has_nested_dp,
            drv_outputs,
        )

        if should_trampoline:
            await self._fire_trampoline(build, build_resp, sched_req, parent_dps)
            trampolined_dps.update(parent_dps)

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

    async def on_build_complete_failed(
        self,
        build: QueuedBuild,
        error_msg: str,
    ) -> None:
        """Handle build failure within a SchedulerBuildRequest."""
        if build.scheduler_request_id is None:
            return
        sched_req = self.queue.requests.get(build.scheduler_request_id)
        if sched_req is None:
            return

        parent_dps = sched_req.build_to_derived.get(build.id, set())
        failed_result = BuildResult(
            status=BuildResultStatus.MISC_FAILURE,
            error_msg=error_msg,
            times_built=0,
            is_non_deterministic=0,
            start_time=0,
            stop_time=0,
            built_outputs={},
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

    # ── Internal helpers ─────────────────────────────────────────────

    def _should_trampoline(
        self,
        build: QueuedBuild,
        is_dynamic: bool,
        has_nested_dp: bool,
        drv_outputs: dict[DrvOutput, Realisation],
    ) -> bool:
        """Decide whether the trampoline should fire for this build.

        Three conditions must hold:
        1. The derivation has dynamic outputs
        2. The build succeeded (status == 0) and produced outputs
        3. Either a DerivedPath has a nested chain, or another
           queued build depends on this build's .drv output
        """
        if not is_dynamic:
            return False
        if not drv_outputs:
            return False

        has_drv_output = False
        for realisation in drv_outputs.values():
            out_path = realisation.get("outPath", "")
            if out_path:
                out_sp = StorePath(out_path).with_store_prefix()
                if out_sp.is_derivation():
                    has_drv_output = True
                    break

        if not has_drv_output:
            return False

        if has_nested_dp:
            return True

        build_drv_path = StorePath(build.request.drv_path)
        for other_build in self.queue.by_id.values():
            if other_build.is_done:
                continue
            if not other_build.dynamic_input_drvs:
                continue
            if build_drv_path in other_build.dynamic_input_drvs:
                return True

        return False

    async def _fire_trampoline(
        self,
        build: QueuedBuild,
        build_resp: BuildDerivationResponse,
        sched_req: SchedulerBuildRequest,
        parent_dps: set[DerivedPath],
    ) -> None:
        """Enqueue inner builds for .drv outputs produced by a dynamic build."""
        drv_outputs = build_resp.result.built_outputs
        for realisation in drv_outputs.values():
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
                inner_parsed = await self.read_drv_fn(
                    self.local_store.store_path,
                    out_sp,
                )
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

            inner_basic = await to_basic_derivation(
                inner_parsed,
                self.local_store.store_path,
            )

            unknown_srcs = inner_basic.input_srcs - self.local_store.tracker.known_paths
            if unknown_srcs:
                try:
                    valid_resp = await self.local_store.execute(
                        QueryValidPathsRequest(paths=unknown_srcs),
                    )
                    self.local_store.tracker.add_known_paths(
                        valid_resp.paths,
                        update_regtime=False,
                    )
                except Exception:
                    log.exception(
                        "trampoline_unknown_srcs_check_failed",
                        build_id=build.id,
                        inner_drv_path=out_sp,
                    )

            inner_req = BuildDerivationRequest(
                drv_path=out_sp,
                derivation=inner_basic,
                build_mode=sched_req.build_mode,
            )

            required_paths: dict[StorePath, UnkeyedValidPathInfo] = {}
            for inp in inner_basic.input_srcs:
                required_paths[StorePath(inp)] = UnkeyedValidPathInfo()
            required_paths[out_sp] = UnkeyedValidPathInfo()

            inner_build_id, _inner_future = await self.scheduler.build_derivation(
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

            self._link_dynamic_deps(build, inner_build_id, inner_basic)

    def _link_dynamic_deps(
        self,
        outer_build: QueuedBuild,
        inner_build_id: BuildId,
        inner_derivation: BasicDerivation,
    ) -> None:
        """After trampoline enqueues an inner build, add DAG edges from
        dependent builds to the inner build, and add the inner build's
        output paths to their required_paths.
        """
        outer_drv_path = StorePath(outer_build.request.drv_path)
        inner_outputs = inner_derivation.output_paths()
        inner_output_paths: StorePathSet = {p for p in inner_outputs.values() if p != StorePath("")}

        for other_build in self.queue.by_id.values():
            if other_build.is_done:
                continue
            if not other_build.dynamic_input_drvs:
                continue
            if outer_drv_path not in other_build.dynamic_input_drvs:
                continue

            if inner_build_id not in other_build.depends_on:
                other_build.depends_on.add(inner_build_id)
                log.info(
                    "dynamic_dep_linked",
                    dependent_build_id=other_build.id,
                    inner_build_id=inner_build_id,
                    outer_build_id=outer_build.id,
                )

            for p in inner_output_paths:
                if p not in other_build.required_paths:
                    other_build.required_paths[p] = UnkeyedValidPathInfo()
                    log.debug(
                        "dynamic_dep_required_path_added",
                        dependent_build_id=other_build.id,
                        path=p,
                    )
