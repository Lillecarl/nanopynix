"""Handler for opaque (non-derivation) store paths.

An opaque path is just a store path that should exist.  The handler
checks if it's already valid, tries to substitute it from caches,
or marks it as unavailable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from pynixd.operations.is_valid_path import IsValidPathRequest
from pynixd.types.build import BuildResult, BuildResultStatus

from ..derived_path import DerivedPath
from .goal import EndGoal, GoalResult
from .handler import GoalHandler

if TYPE_CHECKING:
    from .goal import Goal

log = structlog.get_logger(__name__)


class OpaqueHandler(GoalHandler):
    """Resolve an opaque store path — check validity or substitute."""

    async def execute(self, goal: Goal) -> None:
        assert goal.derived_path.is_opaque
        log.info(
            "execute_opaque",
            derived_path=goal.derived_path.derived,
        )

        path = goal.derived_path.base_store_path()

        if (await goal.ctx.store.execute(IsValidPathRequest(path=path))).valid:
            goal.result = GoalResult(
                path=goal.derived_path,
                result=BuildResult(status=BuildResultStatus.ALREADY_VALID),
                produced_paths={path},
            )
            return

        if info := await goal.ctx.substitution_manager.query_path(path):
            if goal.ctx.end_goal is EndGoal.QUERY:
                goal.result = GoalResult(
                    path=goal.derived_path,
                    result=BuildResult(status=BuildResultStatus.SUBSTITUTED),
                )
                return

            for ref in info.references:
                if path == ref:
                    continue  # don't create a child for itself
                goal.add_child(DerivedPath(ref))

            await goal.execute_children()

            log.info("substituting", path=path)
            await goal.ctx.substitution_manager.substitute_paths({path}, goal.ctx.store)
            goal.result = GoalResult(
                path=goal.derived_path,
                result=BuildResult(status=BuildResultStatus.SUBSTITUTED),
                produced_paths={path},
            )
        else:
            goal.result = GoalResult(
                path=goal.derived_path,
                result=BuildResult(
                    status=BuildResultStatus.UNKNOWN
                    if goal.ctx.end_goal is EndGoal.QUERY
                    else BuildResultStatus.NO_SUBSTITUTERS
                ),
            )
