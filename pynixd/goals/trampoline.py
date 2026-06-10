"""DerivationTrampolineGoal — resolve a DerivedPath to concrete outputs.

For opaque paths: creates a PathSubstitutionGoal.
For nested paths: strips layers recursively.
For flat derivation paths: reads .drv, fans out per-output DerivationGoals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ..types.build import BuildResult, BuildResultStatus
from .goal import EndGoal, Goal, GoalContext, GoalResult

if TYPE_CHECKING:
    from ..derived_path import DerivedPath
    from ..store_path import StorePath

log = structlog.get_logger(__name__)


class DerivationTrampolineGoal(Goal):
    """Resolve a DerivedPath to concrete outputs.

    For opaque paths: creates a PathSubstitutionGoal.
    For nested paths: strips layers recursively.
    For flat derivation paths: reads .drv, fans out per-output DerivationGoals.
    """

    def __init__(self, derived_path: DerivedPath, ctx: GoalContext) -> None:
        super().__init__(ctx)
        self.derived_path = derived_path

    async def execute(self) -> None:
        dp = self.derived_path

        # ── Opaque path ──
        if dp.is_opaque:
            goal = self._make_goal(dp)
            self.add_child(goal)
            await self.execute_children()
            self.result = goal.result
            return

        # ── Nested path ──
        if dp.is_nested:
            await self._resolve_nested(dp)
            return

        # ── Flat derivation path ──
        await self._resolve_flat(dp)

    # ── Nested resolution ──────────────────────────────────────────

    async def _resolve_nested(self, dp: DerivedPath) -> None:
        """Strip one layer, resolve outer, then recurse on remainder."""
        assert dp.is_nested

        log.info("trampoline_nested", derived_path=dp.derived, chain=dp.chain)

        # 1. Build the outer path (e.g. a.drv!out)
        outer_dp = dp.outer
        outer_goal = self._make_goal(outer_dp)
        self.add_child(outer_goal)
        await self.execute_children()

        outer_result = outer_goal.result
        if outer_result is None or not outer_result.produced_paths:
            log.warning("trampoline_outer_failed", outer=outer_dp.derived)
            self.result = GoalResult(
                path=dp,
                result=BuildResult(
                    status=BuildResultStatus.UNKNOWN
                    if self.ctx.end_goal is EndGoal.QUERY
                    else BuildResultStatus.MISC_FAILURE,
                ),
            )
            return

        # 2. Resolve the inner .drv from the outer result
        if not outer_result.resolved_outputs:
            log.warning("trampoline_outer_no_resolved", outer=outer_dp.derived)
            self.result = GoalResult(
                path=dp,
                result=BuildResult(
                    status=BuildResultStatus.UNKNOWN
                    if self.ctx.end_goal is EndGoal.QUERY
                    else BuildResultStatus.MISC_FAILURE,
                ),
            )
            return

        inner_drv = outer_result.resolved_outputs.get(dp.chain[-1])
        if inner_drv is None:
            log.warning(
                "trampoline_chain_output_not_found",
                outer=outer_dp.derived,
                output_name=dp.chain[-1],
            )
            self.result = GoalResult(
                path=dp,
                result=BuildResult(
                    status=BuildResultStatus.UNKNOWN
                    if self.ctx.end_goal is EndGoal.QUERY
                    else BuildResultStatus.MISC_FAILURE,
                ),
            )
            return

        log.debug("trampoline_inner_drv_resolved", inner_drv=inner_drv)

        # 3. Check if inner_drv is a valid derivation
        inner_drv_path = await self._resolve_drv_target(inner_drv)
        if inner_drv_path is None:
            # Not a valid derivation — this IS the final output
            self.result = outer_result
            self.result.path = dp
            return
        inner_drv = inner_drv_path

        # 4. Wrap and build the remainder
        next_dp = dp.wrap(inner_drv)
        remainder_goal = self._make_goal(next_dp)
        self.add_child(remainder_goal)
        await self.execute_children()

        remainder_result = remainder_goal.result
        if remainder_result:
            self.result = remainder_result
            self.result.path = dp
            self.result.produced_paths.add(inner_drv)

    async def _resolve_drv_target(self, candidate: StorePath) -> StorePath | None:
        """Verify ``candidate`` is a valid derivation."""
        if not candidate.is_derivation():
            return None

        try:
            parsed = await self.ctx.store.read_derivation(candidate)
        except Exception:
            return None

        if parsed is None:
            return None

        return candidate

    # ── Goal creation helper ───────────────────────────────────────

    def _make_goal(self, dp: DerivedPath) -> Goal:
        """Create the right goal for a DerivedPath.

        For opaque paths: PathSubstitutionGoal.
        For nested paths: another DerivationTrampolineGoal (recursive).
        For flat derivations: DerivationGoal (no trampoline recursion).
        """
        if dp.is_opaque:
            from .path_substitution import PathSubstitutionGoal

            return PathSubstitutionGoal(derived_path=dp, ctx=self.ctx)
        if dp.is_nested:
            return DerivationTrampolineGoal(derived_path=dp, ctx=self.ctx)
        from ._helpers import _single_output
        from .derivation import DerivationGoal

        return DerivationGoal(
            drv_path=dp.base_store_path(),
            output_name=_single_output(dp),
            ctx=self.ctx,
        )

    # ── Flat derivation resolution ─────────────────────────────────

    async def _resolve_flat(self, dp: DerivedPath) -> None:
        """Resolve flat derivation: create one DerivationGoal per output."""
        goal = self._make_goal(dp)
        self.add_child(goal)
        await self.execute_children()
        self.result = goal.result
