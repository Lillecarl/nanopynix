"""Read-only QueryMissing planning goals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from ..derived_path import DerivedPath
from ..serde import IsValidPathRequest, QueryMissingRequest, QueryMissingResponse
from ..serde import StorePath as SerdeStorePath
from ..store_path import StorePath
from ..substitution_queue import SubstitutionAvailability
from .goal import ExecutionGoal

if TYPE_CHECKING:
    from .engine import GoalEngine

log = structlog.get_logger(__name__)


@dataclass
class QueryMissingPlanGoal(ExecutionGoal[QueryMissingResponse]):
    """Read-only root goal for QueryMissing.

    This preserves the current limited classification behavior while moving it
    behind the goal-system read-only entrypoint. Later slices should teach this
    planner about substituter availability and dependency walking.
    """

    engine: GoalEngine
    request: QueryMissingRequest

    def __post_init__(self) -> None:
        ExecutionGoal.__init__(self, self.engine)

    async def _run(self) -> QueryMissingResponse:
        will_build: set[SerdeStorePath] = set()
        will_substitute: set[SerdeStorePath] = set()
        unknown: set[SerdeStorePath] = set()
        download_size = 0
        nar_size = 0

        for wire_path in self.request.derived_paths:
            derived_path = DerivedPath(wire_path.value)
            base_path = derived_path.base_store_path()
            if base_path.is_derivation():
                will_build.add(SerdeStorePath(path=str(base_path)))
                continue

            response = await self.engine.ctx.local_store.execute(
                IsValidPathRequest(path=SerdeStorePath(path=str(base_path)))
            )
            if not response.valid:
                availability = await self._can_substitute(base_path)
                if availability.available:
                    will_substitute.add(SerdeStorePath(path=str(base_path)))
                    download_size += availability.download_size or 0
                    nar_size += availability.nar_size or 0
                else:
                    unknown.add(SerdeStorePath(path=str(base_path)))

        log.debug(
            "query_missing_goal_plan",
            requested=len(self.request.derived_paths),
            will_build=len(will_build),
            will_substitute=len(will_substitute),
            unknown=len(unknown),
        )
        return QueryMissingResponse(
            will_build=will_build,
            will_substitute=will_substitute,
            unknown=unknown,
            download_size=download_size,
            nar_size=nar_size,
        )

    async def _can_substitute(self, path: StorePath) -> SubstitutionAvailability:
        scheduler = self.engine.ctx.scheduler
        if scheduler is None:
            return SubstitutionAvailability.unavailable()
        return await scheduler.substitution_queue.can_substitute(path)
