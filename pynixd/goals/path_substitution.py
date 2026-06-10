"""BuildGoal for opaque (non-derivation) store paths.

An opaque path is just a store path that should exist.  The goal
checks validity, tries substitution, or marks it as unavailable.
"""

from __future__ import annotations

import structlog

from ..derived_path import DerivedPath
from ..operations.is_valid_path import IsValidPathRequest
from ..store_path import StorePath
from ..types.build import BuildResult, BuildResultStatus
from .goal import EndGoal, Goal, GoalContext, GoalResult, make_build_goal

log = structlog.get_logger(__name__)


class PathSubstitutionGoal(Goal):
    """Resolve an opaque store path — check validity or substitute."""

    def __init__(self, derived_path: DerivedPath, ctx: GoalContext) -> None:
        super().__init__(ctx)
        self._derived_path = derived_path

    async def execute(self) -> None:
        dp = self._derived_path
        assert dp.is_opaque

        log.info("execute_path_substitution", derived_path=dp.derived)

        path = dp.base_store_path()

        # 1. Check validity
        if (await self.ctx.store.execute(IsValidPathRequest(path=path))).valid:
            self.result = GoalResult(
                path=dp,
                result=BuildResult(status=BuildResultStatus.ALREADY_VALID),
                produced_paths={path},
            )
            return

        # 2. Try substitution
        info = await self.ctx.substitution_manager.query_path(path)
        log.debug(
            "DEBUG_opaque_query_path",
            path=str(path),
            got_info=bool(info),
            refs=[str(r) for r in info.references] if info else None,
        )
        if info:
            if self.ctx.end_goal is EndGoal.QUERY:
                self.result = GoalResult(
                    path=dp,
                    result=BuildResult(status=BuildResultStatus.SUBSTITUTED),
                )
                return

            # Resolve references (they must be valid before substitution)
            for ref in info.references:
                if path == ref:
                    continue
                child = make_build_goal(
                    DerivedPath._from_components(
                        drv_path=StorePath(str(ref)),
                        chain=(),
                        outputs=None,
                    ),
                    self.ctx,
                )
                registered = self.ctx.goal_manager.register(child)
                self.add_child(registered)

            await self.execute_children()

            log.info("substituting", path=path)
            await self.ctx.substitution_manager.substitute_paths(
                {path},
                self.ctx.store,
            )
            self.result = GoalResult(
                path=dp,
                result=BuildResult(status=BuildResultStatus.SUBSTITUTED),
                produced_paths={path},
            )
        else:
            self.result = GoalResult(
                path=dp,
                result=BuildResult(
                    status=BuildResultStatus.UNKNOWN
                    if self.ctx.end_goal is EndGoal.QUERY
                    else BuildResultStatus.NO_SUBSTITUTERS,
                ),
            )
