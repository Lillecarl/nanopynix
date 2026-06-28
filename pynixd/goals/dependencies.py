"""Dependency fan-out goals."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .goal import ExecutionGoal, Goal
from .results import GoalResult

if TYPE_CHECKING:
    from .engine import GoalEngine


@dataclass
class DependencyGroupGoal(ExecutionGoal[list[GoalResult]]):
    engine: GoalEngine
    children: Sequence[Goal[GoalResult]]

    def __post_init__(self) -> None:
        ExecutionGoal.__init__(self, self.engine)

    async def _run(self) -> list[GoalResult]:
        return await self.run_children(self.children)
