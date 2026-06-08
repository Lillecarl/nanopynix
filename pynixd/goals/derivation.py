"""Handler for regular (non-CA) derivations where output paths are known.

Reads the derivation from the store, checks if the output already
exists, tries substitution, resolves input children, then builds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

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

        # Delegate to CA handler if the output path isn't known upfront
        if output is None or not output.path:
            await CADerivationHandler(derivation).execute(goal)
            return

        # ── Already valid? ──
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

        # ── Resolve all input dependencies ──
        for path, outputs in derivation.input_drvs.items():
            for out_name in outputs:
                goal.add_child(DerivedPath(f"{path}!{out_name}"))
        for path in derivation.input_srcs:
            goal.add_child(DerivedPath(path))

        await goal.execute_children()

        # ── Collect resolved input paths ──
        input_srcs: set[StorePath] = set()
        for result in goal.collect_results():
            if not isinstance(result, KeyedBuildResult):
                continue
            if isinstance(result, GoalResult):
                input_srcs.update(result.produced_paths)
            input_srcs.update(output.out_path for output in result.result.built_outputs.values())

        # ── Build ──
        if goal.ctx.end_goal is EndGoal.QUERY:
            goal.result = GoalResult(
                path=goal.derived_path,
                result=BuildResult(status=BuildResultStatus.MISC_FAILURE),
            )
            return

        log.info("building", derivation=goal.derived_path.drv_path, input_srcs=input_srcs)
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
                    input_srcs=input_srcs,
                    args=derivation.args,
                    builder=derivation.builder,
                    env=derivation.env,
                    is_dynamic=derivation.is_dynamic,
                    platform=derivation.platform,
                ),
                build_mode=BuildMode.NORMAL,
            )
        )
        goal.result = GoalResult(
            path=goal.derived_path,
            result=response.result,
            produced_paths={StorePath(o.path) for o in derivation.outputs if o.path},
        )
        for realisation in goal.result.result.built_outputs.values():
            await goal.ctx.store.execute(RegisterDrvOutputRequest(realisation=realisation))
