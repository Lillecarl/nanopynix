from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from .operations.ca_derivations import RegisterDrvOutputRequest
from .operations.query_valid_paths import QueryValidPathsRequest
from .operations.build_derivation import (
    BuildDerivationRequest,
)
from .operations.add_to_store import AddToStoreRequest
from .operations.base import OutputKind, BuildResult, BuildResultStatus, UnkeyedValidPathInfo
from .derivation_resolution import (
    _nix_drv_name,
    _unparse_basic_derivation,
    resolve_derivation as drv_resolve_derivation,
    resolve_dynamic_derivation as drv_resolve_dynamic_derivation,
)
from .drv_parser import read_drv_file, to_basic_derivation
from .store_path import StorePath
from .derived_path import DerivedPath

if TYPE_CHECKING:
    from .scheduler import Scheduler, DerivationReader
    from .build_queue import QueuedBuild
    from .store import Store
    from .operations.build_derivation import BuildDerivationResponse
    from .operations.base import BasicDerivation

log = structlog.get_logger(__name__)


class DynamicDerivationResolver:
    """Handles resolution of CA and dynamic derivations during the build lifecycle."""

    def __init__(
        self, scheduler: Scheduler, read_drv_fn: DerivationReader | None = None
    ) -> None:
        self.scheduler = scheduler
        self.local_store = scheduler.local_store
        self.queue = scheduler.queue
        self.read_drv_fn = read_drv_fn or read_drv_file

    async def register_dep_realisations(self, build: QueuedBuild, store: Store) -> None:
        """Register CA realisations from completed dependency builds on the
        target builder store so it can resolve deferred output paths.
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

    async def register_built_outputs(
        self,
        build: QueuedBuild,
        resp: BuildDerivationResponse,
    ) -> None:
        """Register CA realisations from a completed build on the local store."""
        if not resp.result.built_outputs:
            return

        for drv_output_str, realisation in resp.result.built_outputs.items():
            try:
                reg_req = RegisterDrvOutputRequest(realisation=realisation)
                await self.local_store.execute(reg_req, suppress_last=True)
            except Exception:
                log.warning(
                    "register_drv_output_failed",
                    drv_output=drv_output_str,
                    exc_info=True,
                )

    async def resolve_deferred_derivation(
        self, build: QueuedBuild, store: Store
    ) -> None:
        """Resolve a deferred derivation before building."""

        if not build.depends_on:
            return

        if not any(
            o.kind == OutputKind.DEFERRED
            for o in build.request.derivation.outputs.values()
        ):
            return

        drv_path = build.request.drv_path

        try:
            parsed = self.read_drv_fn(self.local_store.store_path, drv_path)
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
            resolved = drv_resolve_derivation(parsed, drv_path, resolved_output_paths)
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

        build.required_paths[resolved_drv_path] = UnkeyedValidPathInfo()
        for p in resolved_output_paths.values():
            if p not in build.required_paths:
                build.required_paths[p] = UnkeyedValidPathInfo()

        for inp in resolved.input_srcs:
            sp = StorePath(inp)
            if sp not in build.required_paths:
                build.required_paths[sp] = UnkeyedValidPathInfo()
        for name, o in resolved.outputs.items():
            if o.path:
                sp = StorePath(o.path)
                if sp not in build.required_paths:
                    build.required_paths[sp] = UnkeyedValidPathInfo()

        log.info(
            "resolved_deferred_derivation",
            build_id=build.id,
            drv_path=drv_path,
            resolved_drv_path=resolved_drv_path,
            output_paths={n: o.path for n, o in resolved.outputs.items()},
        )

    async def resolve_dynamic_derivation(
        self, build: QueuedBuild, store: Store
    ) -> None:
        """Resolve a dynamic (DrvWithVersion) wrapper derivation before building."""

        if not build.depends_on:
            return

        drv_path = build.request.drv_path

        try:
            parsed = self.read_drv_fn(self.local_store.store_path, drv_path)
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

                        # If not in dep_realisations, it might be a standard derivation
                        # that was already built or enqueued elsewhere.
                        if not actual_path:
                            try:
                                inner_parsed = self.read_drv_fn(
                                    self.local_store.store_path, level1_path
                                )
                                inner_outs = inner_parsed.output_paths()
                                actual_path = inner_outs.get(inner_output_name)
                            except Exception:
                                pass

                        if actual_path:
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

        # Add all dynamic outputs to required_paths
        for p in dynamic_output_paths.values():
            if p not in build.required_paths:
                build.required_paths[p] = UnkeyedValidPathInfo()

        try:
            resolved = drv_resolve_dynamic_derivation(
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

        build.required_paths[resolved_drv_path] = UnkeyedValidPathInfo()
        for p in dynamic_output_paths.values():
            if p not in build.required_paths:
                build.required_paths[p] = UnkeyedValidPathInfo()

        for inp in resolved.input_srcs:
            sp = StorePath(inp)
            if sp not in build.required_paths:
                build.required_paths[sp] = UnkeyedValidPathInfo()
        for name, o in resolved.outputs.items():
            if o.path:
                sp = StorePath(o.path)
                if sp not in build.required_paths:
                    build.required_paths[sp] = UnkeyedValidPathInfo()

        log.info(
            "resolved_dynamic_derivation",
            build_id=build.id,
            drv_path=drv_path,
            resolved_drv_path=resolved_drv_path,
            output_paths={n: o.path for n, o in resolved.outputs.items()},
        )

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
                    inner_parsed = self.read_drv_fn(self.local_store.store_path, out_sp)
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

                # Link dependent builds to the inner build via DAG.
                # Builds with dynamic_input_drvs referencing the outer
                # build's drv need to depend on the inner build too,
                # and need its output paths in required_paths.
                self.link_dynamic_deps(build, inner_build_id, inner_basic)

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

    def link_dynamic_deps(
        self,
        outer_build: QueuedBuild,
        inner_build_id: int,
        inner_derivation: BasicDerivation,
    ) -> None:
        """After trampoline enqueues an inner build, add DAG edges from
        dependent builds to the inner build, and add the inner build's
        output paths to their required_paths.
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
                    other_build.required_paths[p] = UnkeyedValidPathInfo()
                    log.debug(
                        "dynamic_dep_required_path_added",
                        dependent_build_id=other_build.id,
                        path=p,
                    )
