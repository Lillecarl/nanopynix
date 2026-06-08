"""Handler for content-addressed derivations (output path unknown at start).

CA derivations don't have known output paths upfront.  The handler
resolves children first, attempts substitution by ``DrvOutput`` key
for fixed-output derivations, and falls back to building via the
daemon.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from pynixd.operations.build_derivation import BuildDerivationRequest
from pynixd.operations.ca_derivations import QueryRealisationRequest, RegisterDrvOutputRequest
from pynixd.operations.is_valid_path import IsValidPathRequest
from pynixd.types import BasicDerivation, BuildMode, DerivationOutput
from pynixd.types.build import BuildResult, BuildResultStatus

from ..derived_path import DerivedPath
from ..store_path import DrvOutput, StorePath
from .goal import EndGoal, GoalResult
from .handler import GoalHandler

if TYPE_CHECKING:
    from ..drv_parser import Derivation
    from ..types.ca import Realisation
    from .goal import Goal

log = structlog.get_logger(__name__)


class CADerivationHandler(GoalHandler):
    """Build (or substitute) a content-addressed derivation.

    Flow:
    1. Resolve all input children (input_drvs + input_srcs)
    2. Collect resolved paths into ``input_srcs``
    3. Try substitution by ``DrvOutput`` (fixed-output / text-hashed only)
    4. Fall back to building via ``BuildDerivationRequest``
    5. Register returned realisations so downstream derivations can find them
    """

    def __init__(self, derivation: Derivation) -> None:
        self._derivation = derivation

    async def execute(self, goal: Goal) -> None:
        derivation = self._derivation
        log.info(
            "execute_ca_derivation",
            derived_path=goal.derived_path.derived,
            is_dynamic=derivation.is_dynamic,
        )

        # ── 1. Resolve all input children ──
        for path, outputs in derivation.input_drvs.items():
            for output in outputs:
                goal.add_child(DerivedPath(f"{path}!{output}"))

        for path, outputs in derivation.dynamic_input_drvs.items():
            for output in outputs:
                goal.add_child(DerivedPath(f"{path}!{output}"))

        await goal.execute_children()

        # ── 2. Collect resolved input paths from children ──
        input_srcs: set[StorePath] = set(derivation.input_srcs)
        for result in goal.collect_results():
            if isinstance(result, GoalResult):
                input_srcs.update(result.produced_paths)

        # Also add any input_srcs that are valid (they're just files, not build targets)
        for src in derivation.input_srcs:
            if src not in input_srcs:
                valid = (await goal.ctx.store.execute(IsValidPathRequest(path=src))).valid
                if valid:
                    input_srcs.add(src)

        # ── 3. Try substitution by DrvOutput ──
        drv_outputs: set[DrvOutput] = set()
        for out in derivation.outputs:
            if out.hash_algo and not out.hash_algo.startswith("r:") and out.hash_value:
                drv_outputs.add(DrvOutput(hash_algo=out.hash_algo, hash_value=out.hash_value, output_name=out.name))

        if drv_outputs:
            # Check the local store first (the CA child may have just built this)
            local_realisations: dict[DrvOutput, Realisation] = {}
            for do in drv_outputs:
                try:
                    resp = await goal.ctx.store.execute(QueryRealisationRequest(drv_output=do))
                    for r in resp.realisations:
                        local_realisations[do] = r
                except Exception:
                    pass

            if local_realisations:
                for realisation in local_realisations.values():
                    await goal.ctx.store.execute(RegisterDrvOutputRequest(realisation=realisation))
                produced_paths: set[StorePath] = set()
                for realisation in local_realisations.values():
                    if out_path := realisation.out_path:
                        produced_paths.add(out_path.with_store_prefix())
                if produced_paths:
                    log.info("ca_local_resolved", produced_paths=produced_paths)
                    goal.result = GoalResult(
                        path=goal.derived_path,
                        result=BuildResult(status=BuildResultStatus.ALREADY_VALID),
                        produced_paths=produced_paths,
                    )
                    return

            # Fall back to checking substituters
            realisations = await goal.ctx.substitution_manager.query_realisations(drv_outputs)
            if realisations:
                for realisation in realisations.values():
                    await goal.ctx.store.execute(RegisterDrvOutputRequest(realisation=realisation))

                produced_paths: set[StorePath] = set()
                for realisation in realisations.values():
                    if out_path := realisation.out_path:
                        produced_paths.add(out_path.with_store_prefix())

                if produced_paths:
                    log.info(
                        "substituted_ca",
                        derivation=goal.derived_path.drv_path,
                        produced_paths=produced_paths,
                    )
                    goal.result = GoalResult(
                        path=goal.derived_path,
                        result=BuildResult(status=BuildResultStatus.SUBSTITUTED),
                        produced_paths=produced_paths,
                    )
                    return

        # ── 4. Build via daemon ──
        if goal.ctx.end_goal is EndGoal.QUERY:
            goal.result = GoalResult(
                path=goal.derived_path,
                result=BuildResult(status=BuildResultStatus.MISC_FAILURE),
            )
            return

        log.info(
            "building_ca",
            derivation=goal.derived_path.drv_path,
            input_count=len(input_srcs),
            input_srcs=input_srcs,
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

        # ── 5. Register realisations ──
        for realisation in response.result.built_outputs.values():
            log.debug(
                "registering_ca_output",
                drv_output=realisation.id,
                out_path=realisation.out_path,
            )
            await goal.ctx.store.execute(RegisterDrvOutputRequest(realisation=realisation))

        # ── 6. Extract produced paths ──
        produced_paths: set[StorePath] = set()
        for realisation in response.result.built_outputs.values():
            if out_path := realisation.out_path:
                produced_paths.add(out_path.with_store_prefix())

        goal.result = GoalResult(
            path=goal.derived_path,
            result=response.result,
            produced_paths=produced_paths,
        )
