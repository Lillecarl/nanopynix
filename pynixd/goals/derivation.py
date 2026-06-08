"""Handler for regular (non-CA) derivations where output paths are known.

Reads the derivation from the store, checks if the output already
exists, tries substitution, resolves input children, then builds.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import structlog

from pynixd.derivation_resolution import (
    _make_store_path,
    _nix_drv_name,
    _output_path_name,
    _rewrite_strings,
    _unparse_derivation_for_hash,
    downstream_placeholder,
    downstream_placeholder_unknown_derivation,
)
from pynixd.operations.build_derivation import BuildDerivationRequest
from pynixd.operations.ca_derivations import RegisterDrvOutputRequest
from pynixd.operations.is_valid_path import IsValidPathRequest
from pynixd.types import BasicDerivation, BuildMode, DerivationOutput, KeyedBuildResult
from pynixd.types.build import BuildResult, BuildResultStatus

from ..derived_path import DerivedPath, OutputsNames
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

        # Delegate to CA handler only for genuinely content-addressed outputs
        if output is not None and output.is_ca:
            await CADerivationHandler(derivation).execute(goal)
            return

        # ── Resolve input derivation children ──
        for path, outputs in derivation.input_drvs.items():
            for out_name in outputs:
                goal.add_child(DerivedPath(f"{path}!{out_name}"))

        for path, outputs in derivation.dynamic_input_drvs.items():
            for out_name in outputs:
                goal.add_child(DerivedPath(f"{path}!{out_name}"))

        await goal.execute_children()

        # ── Collect resolved input paths from children ──
        input_srcs: set[StorePath] = set(derivation.input_srcs)
        for result in goal.collect_results():
            if isinstance(result, GoalResult):
                input_srcs.update(result.produced_paths)

        # input_srcs are plain store paths (may include .drv files).
        # They only need to exist — they're not build targets.
        for src in derivation.input_srcs:
            if src not in input_srcs:
                valid = (await goal.ctx.store.execute(IsValidPathRequest(path=src))).valid
                if valid:
                    input_srcs.add(src)

        # ── Build placeholder → actual_path rewrite map from children ──
        resolved_output_paths: dict[str, StorePath] = {}
        for result in goal.collect_results():
            if not isinstance(result, KeyedBuildResult):
                continue
            for drv_out, realisation in result.result.built_outputs.items():
                if drv_out.output_name and drv_out.output_name not in resolved_output_paths:
                    resolved_output_paths[drv_out.output_name] = realisation.out_path

        drv_path = goal.derived_path.base_store_path()

        # ── Handle dynamic_input_drvs: build inner .drv files ──
        if derivation.dynamic_input_drvs:
            for output_map in derivation.dynamic_input_drvs.values():
                for outer_out_name, inner_out_names in output_map.items():
                    drv_file_path = resolved_output_paths.get(outer_out_name)
                    if drv_file_path and drv_file_path.is_derivation():
                        for inner_out_name in inner_out_names:
                            inner_dp = DerivedPath(f"{drv_file_path}!{inner_out_name}")
                            goal.add_child(inner_dp)

            await goal.execute_children()

            for result in goal.collect_results():
                if not isinstance(result, KeyedBuildResult):
                    continue
                for drv_out, realisation in result.result.built_outputs.items():
                    if drv_out.output_name and drv_out.output_name not in resolved_output_paths:
                        resolved_output_paths[drv_out.output_name] = realisation.out_path

        rewrites: dict[str, str] = {}
        new_input_srcs: set[StorePath] = set(derivation.input_srcs) | input_srcs

        for input_drv_path, output_names in derivation.input_drvs.items():
            for output_name in output_names:
                placeholder = downstream_placeholder(input_drv_path, output_name)
                actual_path = resolved_output_paths.get(output_name)
                if actual_path is not None:
                    rewrites[placeholder] = str(actual_path)
                    new_input_srcs.add(StorePath(str(actual_path)))

        for dpath, output_map in derivation.dynamic_input_drvs.items():
            for outer_out_name, inner_out_names in output_map.items():
                drv_file_path = resolved_output_paths.get(outer_out_name)
                if drv_file_path and drv_file_path.is_derivation():
                    # The env placeholder uses
                    # DownstreamPlaceholder::unknownDerivation:
                    # outer hash = raw hash of the PARENT placeholder
                    # (computed from the original drv path, not resolved)
                    hash_part = str(dpath).rsplit("/", 1)[-1].split("-", 1)[0]
                    parent_drv_name = _nix_drv_name(dpath)
                    outer_clear = (
                        f"nix-upstream-output:{hash_part}:{_output_path_name(parent_drv_name, outer_out_name)}"
                    )
                    outer_hash = hashlib.sha256(outer_clear.encode()).digest()
                    for inner_out_name in inner_out_names:
                        placeholder = downstream_placeholder_unknown_derivation(outer_hash, inner_out_name)
                        actual_path = resolved_output_paths.get(inner_out_name)
                        if actual_path is not None:
                            rewrites[placeholder] = str(actual_path)
                            new_input_srcs.add(StorePath(str(actual_path)))

        # ── Resolved env/args ──
        resolved_builder = _rewrite_strings(derivation.builder, rewrites)
        resolved_args = [_rewrite_strings(a, rewrites) for a in derivation.args]
        resolved_env = {k: _rewrite_strings(v, rewrites) for k, v in derivation.env.items()}

        # ── Deferred derivation: resolve placeholders, compute output path ──
        # 1. Clear input_drvs (daemon replaces them with modulo hashes anyway)
        # 2. Rewrite placeholders in env/args with children's real paths
        # 3. Derivation.serialize() → ATerm matching daemon's hash input
        # 4. Hash ATerm → compute output paths via _make_store_path
        if output is None or not output.path:
            if goal.ctx.end_goal is EndGoal.QUERY:
                goal.result = GoalResult(
                    path=goal.derived_path,
                    result=BuildResult(status=BuildResultStatus.UNKNOWN),
                )
                return

            # ── Build the resolved env/args for the actual build ──
            resolved_builder = _rewrite_strings(derivation.builder, rewrites) if rewrites else derivation.builder
            resolved_args = [_rewrite_strings(a, rewrites) for a in derivation.args] if rewrites else derivation.args
            resolved_env = (
                {k: _rewrite_strings(v, rewrites) for k, v in derivation.env.items()} if rewrites else derivation.env
            )

            # ── Compute modulo hash for each input drv ──
            # Nix's hashDerivationModulo replaces each input_drv entry with
            # the hex hash of the input derivation's own modulo hash.
            # We read each child's .drv and compute its ATerm hash (with
            # empty input_drvs, since Nix recurses the same process).
            input_drv_hashes: dict[str, list[str]] = {}
            for child in goal.children:
                if not child.result or child.result.result.status not in (0, 1, 2):
                    continue
                child_drv = await read_drv_file(
                    goal.ctx.store.store_path,
                    child.derived_path.base_store_path(),
                )
                if child_drv is None:
                    continue
                child_aterm = _unparse_derivation_for_hash(child_drv, {})
                child_hash = hashlib.sha256(child_aterm.encode()).hexdigest()
                if isinstance(child.derived_path.outputs, OutputsNames):
                    for name in child.derived_path.outputs.names:
                        input_drv_hashes.setdefault(child_hash, []).append(name)

            log.info(
                "building_deferred",
                drv_path=str(drv_path),
                input_count=len(input_drv_hashes),
            )
            # ── Serialize with hash-replaced input_drvs and hash ──
            aterm = _unparse_derivation_for_hash(derivation, input_drv_hashes)
            h = hashlib.sha256(aterm.encode()).digest()
            log.info(
                "deferred_aterm",
                aterm_preview=aterm[:400],
                hash_hex=h.hex(),
                input_hashes=input_drv_hashes,
            )

            resolved_outputs: dict[str, DerivationOutput] = {}
            for o in derivation.outputs:
                store_name = _output_path_name(_nix_drv_name(drv_path), o.name)
                out_path = _make_store_path(f"output:{o.name}", h, store_name)
                resolved_outputs[o.name] = DerivationOutput(
                    path=out_path,
                    method=o.hash_algo,
                    hash_digest=o.hash_value,
                )
                resolved_env[o.name] = out_path

            response = await goal.ctx.store.execute(
                BuildDerivationRequest(
                    drv_path=drv_path,
                    derivation=BasicDerivation(
                        outputs=resolved_outputs,
                        input_srcs=new_input_srcs,
                        platform=derivation.platform,
                        builder=resolved_builder,
                        args=resolved_args,
                        env=resolved_env,
                        is_dynamic=derivation.is_dynamic,
                    ),
                    build_mode=BuildMode.NORMAL,
                )
            )
            goal.result = GoalResult(
                path=goal.derived_path,
                result=response.result,
                produced_paths={StorePath(o.path) for o in resolved_outputs.values() if o.path}
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

        log.info(
            "building_known",
            derivation=goal.derived_path.drv_path,
            output_path=output.path,
        )
        response = await goal.ctx.store.execute(
            BuildDerivationRequest(
                drv_path=goal.derived_path.base_store_path(),
                derivation=BasicDerivation(
                    outputs={
                        o.name: DerivationOutput(
                            path=o.path,
                            method=o.hash_algo,
                            hash_digest=o.hash_value,
                        )
                        for o in derivation.outputs
                    },
                    input_srcs=new_input_srcs,
                    platform=derivation.platform,
                    builder=resolved_builder,
                    args=resolved_args,
                    env=resolved_env,
                    is_dynamic=derivation.is_dynamic,
                ),
                build_mode=BuildMode.NORMAL,
            )
        )
        goal.result = GoalResult(
            path=goal.derived_path,
            result=response.result,
            produced_paths={StorePath(output.path)}
            | {r.out_path for r in response.result.built_outputs.values() if r.out_path},
        )
