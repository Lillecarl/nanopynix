"""DerivationGoal — ensure one output of a derivation exists.

Coordinates:
1. Resolution — compute output path via ResolutionGoal child
2. Substitution — try to download via PathSubstitutionGoal or DrvOutputSubstitutionGoal
3. Build — DerivationBuildingGoal as last resort
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from ..drv_parser import DrvOutput as DrvOutputType
from ..operations.is_valid_path import IsValidPathRequest
from ..types import DerivationOutput
from ..types.build import BuildResult, BuildResultStatus
from ..types.derivation import OutputKind
from ._helpers import _collect_dynamic_paths, _fake_dp
from ._helpers import _find_output as _find_dop
from .goal import EndGoal, Goal, GoalContext, GoalResult, make_resolution_goal

if TYPE_CHECKING:
    from ..store_path import StorePath

log = structlog.get_logger(__name__)


class DerivationGoal(Goal):
    """Ensure one output of a derivation exists.

    One goal per (drv_path, output_name). Coordinates:
    1. Resolution — compute output path
    2. Substitution — try to download (PathSubstitutionGoal or DrvOutputSubstitutionGoal)
    3. Build — DerivationBuildingGoal as last resort
    """

    def __init__(
        self,
        drv_path: StorePath,
        output_name: str,
        ctx: GoalContext,
    ) -> None:
        super().__init__(ctx)
        self.drv_path = drv_path
        self.output_name = output_name

    async def execute(self) -> None:
        try:
            await self._execute()
        except Exception as e:
            log.exception(
                "derivation_goal_crashed",
                drv_path=str(self.drv_path),
                output=self.output_name,
                error=str(e),
            )
            self.result = GoalResult(
                path=_fake_dp(self.drv_path, self.output_name),
                result=BuildResult(status=BuildResultStatus.MISC_FAILURE),
            )

    async def _execute(self) -> None:
        log.info(
            "derivation_goal_execute",
            drv_path=self.drv_path,
            output=self.output_name,
        )

        # 1. Read derivation (needed for strategy decisions)
        derivation = await self.ctx.store.read_derivation(self.drv_path)
        if derivation is None:
            self.result = GoalResult(
                path=_fake_dp(self.drv_path, self.output_name),
                result=BuildResult(
                    status=BuildResultStatus.MISC_FAILURE,
                    error_msg=f"Derivation not found: {self.drv_path}",
                ),
            )
            return

        # 2. ResolutionGoal — dependency child
        resolve = make_resolution_goal(self.drv_path, self.output_name, self.ctx)
        registered = self.ctx.goal_manager.register(resolve)
        self.add_child(registered)
        await self.execute_children()

        # If resolution failed → build directly (CA-floating with no prior realisation)
        if registered.result is None or not registered.result.resolved_outputs:
            log.info(
                "derivation_goal_fallback_build",
                drv_path=self.drv_path,
                has_result=registered.result is not None,
                resolved_outputs=registered.result.resolved_outputs if registered.result else None,
            )
            await self._try_build(derivation)
            return

        outpath = registered.result.resolved_outputs.get(self.output_name)
        if outpath is None:
            log.info(
                "derivation_goal_fallback_build_no_resolved",
                drv_path=self.drv_path,
                resolved_outputs=registered.result.resolved_outputs,
            )
            await self._try_build(derivation)
            return

        # 3. Check local validity
        valid_check = await self.ctx.store.execute(IsValidPathRequest(path=outpath))
        log.info(
            "derivation_goal_valid_check",
            drv_path=str(self.drv_path),
            outpath=str(outpath),
            valid=valid_check.valid,
        )
        if valid_check.valid:
            self.result = GoalResult(
                path=_fake_dp(self.drv_path, self.output_name),
                result=BuildResult(status=BuildResultStatus.ALREADY_VALID),
                produced_paths={outpath},
                resolved_outputs={self.output_name: outpath},
                modulo_hash=registered.result.modulo_hash if registered.result else "",
            )
            return

        # 4. Substitution strategy depends on output kind
        output_obj = _find_dop(derivation, self.output_name)
        if output_obj is not None:
            dop = DerivationOutput(
                path=output_obj.path,
                method=output_obj.hash_algo,
                hash_digest=output_obj.hash_value,
            )
            log.info(
                "derivation_goal_strategy",
                drv_path=str(self.drv_path),
                output_name=self.output_name,
                kind=dop.kind if dop else str(None),
                output_path=output_obj.path,
            )
            if dop.kind in (OutputKind.INPUT_ADDRESSED, OutputKind.CA_FIXED):
                # Known output path → try PathSubstitutionGoal directly
                await self._try_path_substitution(outpath)
            elif dop.kind == OutputKind.CA_FLOATING:
                # Unknown path → try DrvOutputSubstitutionGoal first
                await self._try_drv_output_substitution(dop)
            # DEFERRED outputs have no hash info — skip substitution, go to build

        # 5. Build if substitution didn't succeed
        if self.result is None or not self.result.result.status.is_success:
            await self._try_build(derivation)

    # ── Substitution strategies ────────────────────────────────────

    async def _try_path_substitution(self, outpath: StorePath) -> None:
        """Try to substitute a known output path."""
        from ..derived_path import DerivedPath
        from .path_substitution import PathSubstitutionGoal

        psg = PathSubstitutionGoal(
            derived_path=DerivedPath._from_components(
                drv_path=outpath,
                chain=(),
                outputs=None,
            ),
            ctx=self.ctx,
        )
        registered = self.ctx.goal_manager.register(psg)
        self.add_child(registered)
        await self.execute_children()
        if registered.result and registered.result.produced_paths:
            self.result = registered.result

    async def _try_drv_output_substitution(self, dop: Any) -> None:
        """Try to resolve a CA output via realisation lookup."""
        clean_algo = dop.method.removeprefix("r:") if dop.method else dop.method

        drv_output = DrvOutputType(
            hash_algo=clean_algo,
            hash_value=dop.hash_digest,
            output_name=self.output_name,
            path="",
        )
        from .drv_output_substitution import DrvOutputSubstitutionGoal

        dosg = DrvOutputSubstitutionGoal(drv_output, self.ctx)
        registered = self.ctx.goal_manager.register(dosg)
        self.add_child(registered)
        await self.execute_children()
        # Read from REGISTERED goal (may differ if dedup occurred)
        if isinstance(registered, DrvOutputSubstitutionGoal) and registered.output_info:
            await self._try_path_substitution(registered.output_info)

    # ── Build fallback ─────────────────────────────────────────────

    async def _try_build(self, derivation: Any) -> None:
        """Fall back to building the derivation."""
        if self.ctx.end_goal is EndGoal.QUERY:
            self.result = GoalResult(
                path=_fake_dp(self.drv_path, self.output_name),
                result=BuildResult(status=BuildResultStatus.UNKNOWN),
            )
            return

        from .building import DerivationBuildingGoal

        bg = DerivationBuildingGoal(self.drv_path, self.ctx)
        bg.output_name = self.output_name
        bg.derivation = derivation
        from ._helpers import _collect_resolved_paths as _deep_collect
        from .resolution import ResolutionGoal

        # Collect resolved paths from input deps only (skip the current
        # derivation's own ResolutionGoal — its resolved_outputs contain
        # the derivation's own output, not input paths).
        input_dep_resolved: dict[str, StorePath] = {}
        for child in self.children:
            if isinstance(child, ResolutionGoal):
                input_dep_resolved.update(_deep_collect(child.children))
            else:
                input_dep_resolved.update(_deep_collect({child}))
        bg.resolved_paths = input_dep_resolved
        bg.dynamic_paths = _collect_dynamic_paths(self.children)
        bg.input_srcs = self._collect_input_srcs()
        registered = self.ctx.goal_manager.register(bg)
        # register() may return a canonical copy (dedup).  Sync properties.
        if isinstance(registered, DerivationBuildingGoal) and registered is not bg:
            registered.output_name = bg.output_name
            registered.derivation = bg.derivation
            registered.resolved_paths = bg.resolved_paths
            registered.dynamic_paths = bg.dynamic_paths
            registered.input_srcs = bg.input_srcs
        self.add_child(registered)
        await self.execute_children()
        if registered.result:
            self.result = registered.result

    # ── Helpers ────────────────────────────────────────────────────

    def _collect_input_srcs(self) -> set[StorePath]:
        """Collect input sources from children's produced paths."""
        srcs: set[StorePath] = set()
        for child in self.children:
            if child.result:
                srcs.update(child.result.produced_paths)
        return srcs
