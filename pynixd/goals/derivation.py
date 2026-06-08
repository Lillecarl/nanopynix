"""Handler for regular (non-CA) derivations where output paths are known.

Reads the derivation from the store, checks if the output already
exists, tries substitution, resolves input children, then builds.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import structlog

from pynixd.derivation_resolution import (
    _make_output_path,
    _nix_drv_name,
    _rewrite_strings,
    downstream_placeholder,
)
from pynixd.operations.build_derivation import BuildDerivationRequest
from pynixd.operations.ca_derivations import RegisterDrvOutputRequest
from pynixd.operations.is_valid_path import IsValidPathRequest
from pynixd.types import BasicDerivation, BuildMode, DerivationOutput, KeyedBuildResult
from pynixd.types.build import BuildResult, BuildResultStatus

from ..derived_path import DerivedPath
from ..drv_parser import read_drv_file
from ..store_path import StorePath
from .ca_derivation import CADerivationHandler
from .goal import EndGoal, GoalResult
from .handler import GoalHandler

if TYPE_CHECKING:
    from .goal import Goal

log = structlog.get_logger(__name__)


class DerivationHandler(GoalHandler):
    """Build a regular derivation whose output paths are known upfront."""

    async def execute(self, goal: Goal) -> None:
        assert not goal.derived_path.is_opaque

        derivation = await read_drv_file(
            goal.ctx.store.store_path,
            goal.derived_path.base_store_path(),
        )
        if derivation is None:
            log.warning(
                "derivation_not_found",
                drv_path=goal.derived_path.drv_path,
            )
            goal.result = GoalResult(
                path=goal.derived_path,
                result=BuildResult(
                    status=BuildResultStatus.UNKNOWN
                    if goal.ctx.end_goal is EndGoal.QUERY
                    else BuildResultStatus.MISC_FAILURE
                ),
            )
            return

        # Build a lookup: DerivedPath → DerivationOutput
        derived_outputs: dict[DerivedPath, DerivationOutput] = {}
        for out in derivation.outputs:
            dp = DerivedPath(f"{goal.derived_path.base_store_path()}!{out.name}")
            derived_outputs[dp] = DerivationOutput(
                path=out.path,
                method=out.hash_algo,
                hash_digest=out.hash_value,
            )

        output = derived_outputs.get(goal.derived_path)
        log.debug(
            "derivation_output_resolve",
            drv_path=goal.derived_path.base_store_path(),
            derived_output_keys=[str(k) for k in derived_outputs],
            goal_dp=str(goal.derived_path),
            output_is_none=output is None,
            output_path=output.path if output else None,
            output_is_ca=output.is_ca if output else None,
            num_outputs=len(derivation.outputs),
            output_paths_raw=[(o.name, o.path) for o in derivation.outputs],
        )

        # Delegate to CA handler only for genuinely content-addressed outputs
        if output is not None and output.is_ca:
            await CADerivationHandler(derivation).execute(goal)
            return

        # ── Resolve all input dependencies (always needed for CA child paths) ──
        for path, outputs in derivation.input_drvs.items():
            for out_name in outputs:
                goal.add_child(DerivedPath(f"{path}!{out_name}"))
        for path in derivation.input_srcs:
            goal.add_child(DerivedPath(str(path)))

        await goal.execute_children()

        # ── Collect resolved input paths ──
        input_srcs: set[StorePath] = set()
        for result in goal.collect_results():
            if not isinstance(result, KeyedBuildResult):
                continue
            if isinstance(result, GoalResult):
                input_srcs.update(result.produced_paths)
            input_srcs.update(output.out_path for output in result.result.built_outputs.values())

        # Deferred derivation: output path unknown (depends on unresolved CA deps).
        # Resolve the derivation by substituting placeholders with actual paths
        # from the (now-complete) children, then compute output paths.
        if output is None or not output.path:
            if goal.ctx.end_goal is EndGoal.QUERY:
                goal.result = GoalResult(
                    path=goal.derived_path,
                    result=BuildResult(status=BuildResultStatus.UNKNOWN),
                )
                return

            # Build placeholder → actual_path rewrite map from children
            resolved_output_paths: dict[str, StorePath] = {}
            for result in goal.collect_results():
                if not isinstance(result, KeyedBuildResult):
                    continue
                for drv_out, realisation in result.result.built_outputs.items():
                    if drv_out.output_name and drv_out.output_name not in resolved_output_paths:
                        resolved_output_paths[drv_out.output_name] = realisation.out_path

            drv_path = goal.derived_path.base_store_path()

            rewrites: dict[str, str] = {}
            new_input_srcs: set[StorePath] = set(derivation.input_srcs) | input_srcs

            for input_drv_path, output_names in derivation.input_drvs.items():
                for output_name in output_names:
                    placeholder = downstream_placeholder(input_drv_path, output_name)
                    actual_path = resolved_output_paths.get(output_name)
                    if actual_path is None:
                        log.warning(
                            "deferred_resolve_missing_output",
                            input_drv_path=input_drv_path,
                            output_name=output_name,
                            resolved_keys=list(resolved_output_paths),
                        )
                        continue
                    rewrites[placeholder] = str(actual_path)
                    new_input_srcs.add(StorePath(str(actual_path)))

            # Read the raw .drv file, do string replacement of placeholders,
            # then compute the output path by hashing the result.
            drv_name = _nix_drv_name(drv_path)

            # Read raw .drv content: {store_path}/nix/store/{bare}
            drv_raw_path = goal.ctx.store.store_path / str(drv_path)
            raw_aterm = drv_raw_path.read_text()

            # Replace placeholders directly in the ATerm string
            resolved_aterm = raw_aterm
            for placeholder, actual in rewrites.items():
                resolved_aterm = resolved_aterm.replace(placeholder, actual)

            # Compute output path from the resolved ATerm
            h = hashlib.sha256(resolved_aterm.encode()).digest()
            out_path = _make_output_path("out", h, drv_name)

            log.info(
                "building_deferred_resolved",
                derivation=goal.derived_path.drv_path,
                input_count=len(new_input_srcs),
                rewrites=rewrites,
                out_path=out_path,
                drv_name=drv_name,
            )

            # Log original BasicDerivation fields before rewriting
            log.info(
                "building_deferred_original",
                outputs_raw=[(o.name, o.path, o.hash_algo, o.hash_value) for o in derivation.outputs],
                input_srcs_raw=[str(s) for s in derivation.input_srcs],
                builder_raw=derivation.builder,
                args_raw=derivation.args,
                env_raw=derivation.env,
                input_drvs={str(k): v for k, v in derivation.input_drvs.items()},
            )

            resolved = BasicDerivation(
                outputs={
                    o.name: DerivationOutput(
                        path=o.path or out_path,
                        method=o.hash_algo,
                        hash_digest=o.hash_value,
                    )
                    for o in derivation.outputs
                },
                input_srcs=new_input_srcs,
                platform=derivation.platform,
                builder=_rewrite_strings(derivation.builder, rewrites),
                args=[_rewrite_strings(a, rewrites) for a in derivation.args],
                env={k: _rewrite_strings(v, rewrites) for k, v in derivation.env.items()},
                is_dynamic=derivation.is_dynamic,
            )

            log.info(
                "building_deferred_before_send",
                drv_path=str(drv_path),
                outputs={n: o.path for n, o in resolved.outputs.items()},
                input_srcs=[str(s) for s in resolved.input_srcs],
                builder=resolved.builder,
                args=resolved.args,
                env=resolved.env,
                is_dynamic=resolved.is_dynamic,
                platform=resolved.platform,
            )

            response = await goal.ctx.store.execute(
                BuildDerivationRequest(
                    drv_path=drv_path,
                    derivation=resolved,
                    build_mode=BuildMode.NORMAL,
                )
            )
            goal.result = GoalResult(
                path=goal.derived_path,
                result=response.result,
                produced_paths={StorePath(o.path) for o in resolved.outputs.values() if o.path}
                | {r.out_path for r in response.result.built_outputs.values() if r.out_path},
            )
            for realisation in goal.result.result.built_outputs.values():
                await goal.ctx.store.execute(RegisterDrvOutputRequest(realisation=realisation))
            return

        # ── Known output path: check validity / substitute / build ──
        if (await goal.ctx.store.execute(IsValidPathRequest(path=StorePath(output.path)))).valid:
            goal.result = GoalResult(
                path=goal.derived_path,
                result=BuildResult(status=BuildResultStatus.ALREADY_VALID),
                produced_paths={StorePath(output.path)},
            )
            return

        # ── Try substitution ──
        log.info("checking_substituters", path=output.path)
        if await goal.ctx.substitution_manager.query_path(StorePath(output.path)):
            child = goal.add_child(DerivedPath(output.path))
            await goal.execute_children()
            if child.result:
                goal.result = child.result
                goal.result.path = goal.derived_path
            return

        # ── Build ──
        if goal.ctx.end_goal is EndGoal.QUERY:
            goal.result = GoalResult(
                path=goal.derived_path,
                result=BuildResult(status=BuildResultStatus.MISC_FAILURE),
            )
            return
