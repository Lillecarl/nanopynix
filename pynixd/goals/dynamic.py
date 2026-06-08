"""Handler for dynamic derivations (nested ``DerivedPath`` chains).

A nested path like ``a.drv!out!lib`` means:
1. Build ``a.drv``, take output ``out`` — which yields a ``.drv`` file
2. Build that inner ``.drv``, take output ``lib`` — the final result

The handler peels one level of nesting per execution, building the
outer path first, then wrapping the result so the next handler
(either another ``DynamicDerivationHandler`` or ``DerivationHandler``)
resolves the remainder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from pynixd.types.build import BuildResult, BuildResultStatus

from .goal import EndGoal, Goal, GoalResult
from .handler import GoalHandler

if TYPE_CHECKING:
    from ..store_path import StorePath

log = structlog.get_logger(__name__)


class DynamicDerivationHandler(GoalHandler):
    """Resolve a nested derived path one level at a time.

    Delegates the outer level to a child goal, then wraps the result
    for the remaining chain.
    """

    async def execute(self, goal: Goal) -> None:
        dp = goal.derived_path
        assert dp.is_nested

        log.info(
            "execute_dynamic",
            derived_path=dp.derived,
            chain=dp.chain,
        )

        # ── 1. Build the outer (shallower) path ──
        # For a.drv!out!lib, outer = a.drv!out.
        # This builds the outer derivation and produces a .drv file.
        child = goal.add_child(dp.outer)
        await goal.execute_children()

        # ── 2. Find the inner .drv path from the child's result ──
        inner_drv = self._find_inner_drv(child, dp.chain[-1])
        if inner_drv is None:
            log.warning(
                "dynamic_inner_drv_not_found",
                outer=dp.outer,
                output_name=dp.chain[-1],
            )
            goal.result = GoalResult(
                path=dp,
                result=BuildResult(
                    status=BuildResultStatus.UNKNOWN
                    if goal.ctx.end_goal is EndGoal.QUERY
                    else BuildResultStatus.MISC_FAILURE
                ),
            )
            return

        log.debug(
            "dynamic_inner_drv_resolved",
            inner_drv=inner_drv,
        )

        # ── 3. Wrap and build the remainder ──
        # wrap(inner_drv) replaces the root with inner_drv and clears
        # the chain, producing e.g. inner_drv!lib.
        next_dp = dp.wrap(inner_drv)

        # Dispatch to the appropriate handler for the remainder.
        from .goal import _resolve_handler

        handler = _resolve_handler(next_dp)
        remainder = Goal(
            derived_path=next_dp,
            ctx=goal.ctx,
            handler=handler,
        )
        remainder.add_parent(goal)
        await remainder.execute()

        if remainder.result:
            goal.result = remainder.result
            goal.result.path = dp

    @staticmethod
    def _find_inner_drv(
        child: Goal,
        output_name: str,
    ) -> StorePath | None:
        """Extract the inner ``.drv`` path from a completed child goal.

        Tries, in order:
        1. ``produced_paths`` — if exactly one path exists, it's our .drv.
        2. ``built_outputs`` — match by output name from the chain.
        """
        if not child.result:
            return None

        # Strategy 1: single produced path — common case
        if len(child.result.produced_paths) == 1:
            return next(iter(child.result.produced_paths))

        # Strategy 2: search built_outputs by output name
        for drv_output, realisation in child.result.result.built_outputs.items():
            if drv_output.output_name == output_name:
                return realisation.out_path

        return None
