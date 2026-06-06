"""Build goal orchestration.

The GoalManager discovers, schedules, and monitors build goals
across the pynixd build queue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..derived_path import DerivedPath
    from .goal import Goal


class GoalManager:
    """Orchestrates build goals across the system.

    Tracks all active goals in a ``DerivedPath``-keyed dictionary
    and provides scheduling, DAG resolution, and lifecycle management.
    """

    def __init__(self) -> None:
        self.goals: dict[DerivedPath, Goal] = {}
