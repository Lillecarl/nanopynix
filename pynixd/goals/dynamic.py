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

        # ── 2. Find the inner .drv from the outer result ──
        inner_drv = self._find_inner_drv(registered, dp.chain[-1])
        if inner_drv is None:
            log.warning(
                "dynamic_inner_drv_not_found",
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

        # ── 3. Wrap and build the remainder ──
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
            # can find it via ``_find_inner_drv``
            self.result.produced_paths.add(inner_drv)

    # ── Inner .drv discovery ───────────────────────────────────────

    @staticmethod
    def _find_inner_drv(
        outer_goal: Goal,
        output_name: str,
    ) -> StorePath | None:
        """Extract the inner ``.drv`` path from a completed outer goal.

        Tries, in order:
        1. ``produced_paths`` — if exactly one path, it's our .drv.
        2. ``resolved_outputs`` — match by output name.
        3. ``built_outputs`` — match by output name.
        """
        if not outer_goal.result:
            return None

        # Strategy 1: single produced path — likely the .drv
        if len(outer_goal.result.produced_paths) == 1:
            candidate = next(iter(outer_goal.result.produced_paths))
            if candidate.is_derivation():
                return candidate

        # Strategy 2: produced_paths that are derivations
        for sp in outer_goal.result.produced_paths:
            if sp.is_derivation():
                return sp

        # Strategy 3: resolved_outputs
        resolved = outer_goal.result.resolved_outputs.get(output_name)
        if resolved is not None and resolved.is_derivation():
            return resolved

        # Strategy 4: child results
        for child in outer_goal.children:
            if child.result and child.result.produced_paths:
                for sp in child.result.produced_paths:
                    if sp.is_derivation():
                        return sp

        return None


