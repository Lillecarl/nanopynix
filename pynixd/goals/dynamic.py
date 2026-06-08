"""BuildGoal for nested derived path chains (dynamic derivations).

A ``DynamicBuildGoal`` handles paths like ``a.drv!out!lib`` where:

1. Building ``a.drv`` produces a ``.drv`` file as one of its outputs.
2. That inner ``.drv`` must be built to produce the final output.

The handler peels one level of nesting per execution: it builds the
outer path first, then wraps the result into a new ``DerivedPath``
for the remainder of the chain.

Example
-------
``a.drv!out!lib`` → build ``a.drv!out`` → get inner ``b.drv`` →
build ``b.drv!lib`` → final result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ..types.build import BuildResult, BuildResultStatus
from .goal import EndGoal, Goal, GoalContext, GoalKey, GoalResult, make_build_goal

if TYPE_CHECKING:
    from ..derived_path import DerivedPath
    from ..store_path import StorePath

log = structlog.get_logger(__name__)


class DynamicBuildGoal(Goal):
    """Resolve a nested derived path one level at a time.

    Creates a child ``BuildGoal`` for the outer (shallower) path,
    then wraps the result into a new derived path for the remainder.
    """

    def __init__(self, derived_path: DerivedPath, ctx: GoalContext) -> None:
        super().__init__(ctx)
        self._derived_path = derived_path

    @property
    def key(self) -> GoalKey:
        return GoalKey.build(self._derived_path)

    async def execute(self) -> None:
        dp = self._derived_path
        assert dp.is_nested, f"DynamicBuildGoal requires a nested path, got {dp}"

        log.info(
            "execute_dynamic",
            derived_path=dp.derived,
            chain=dp.chain,
        )

        # ── 1. Build the outer path (e.g. a.drv!out) ──
        outer_dp = dp.outer
        outer_goal = make_build_goal(outer_dp, self.ctx)
        registered = self.ctx.goal_manager.register(outer_goal)
        self.add_child(registered)

        await self.execute_children()

        if registered.result is None or not registered.result.produced_paths:
            log.warning(
                "dynamic_outer_failed",
                outer=outer_dp,
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

        # ── 2. Resolve the inner .drv from the outer result ──
        # The chain tells us the next .drv is at resolved_outputs[chain[-1]].
        # No filename heuristic needed — the DerivedPath chain structure
        # already encodes the nesting.  The store path at this chain level
        # IS a valid derivation ATerm regardless of its extension.
        if not registered.result or not registered.result.resolved_outputs:
            log.warning("dynamic_outer_no_resolved", outer=outer_dp)
            self.result = GoalResult(
                path=dp,
                result=BuildResult(
                    status=BuildResultStatus.UNKNOWN
                    if self.ctx.end_goal is EndGoal.QUERY
                    else BuildResultStatus.MISC_FAILURE,
                ),
            )
            return
        inner_drv = registered.result.resolved_outputs.get(dp.chain[-1])
        if inner_drv is None:
            log.warning(
                "dynamic_chain_output_not_found",
                outer=outer_dp,
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

        log.debug("dynamic_inner_drv_resolved", inner_drv=inner_drv)

        # ── 3. Check if inner_drv is actually a derivation ──
        # The daemon wire protocol requires derivation paths to end
        # in ``.drv``.  If the chain collapsed (all levels resolved
        # and we got the final output), skip wrapping.
        if not inner_drv.is_derivation():
            self.result = registered.result
            self.result.path = dp
            return

        # ── 4. Wrap and build the remainder ──
        # ``dp.wrap(inner_drv)`` replaces the root with inner_drv
        # and clears the chain, producing e.g. inner_drv!lib
        next_dp = dp.wrap(inner_drv)

        remainder_goal = make_build_goal(next_dp, self.ctx)
        registered_remainder = self.ctx.goal_manager.register(remainder_goal)
        self.add_child(registered_remainder)
        await self.execute_children()

        if registered_remainder.result:
            self.result = registered_remainder.result
            self.result.path = dp
            # Propagate the inner .drv path so parent DynamicBuildGoals
            # can resolve the chain at the next level up
            self.result.produced_paths.add(inner_drv)


