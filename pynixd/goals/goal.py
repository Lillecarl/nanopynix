"""Build goal representation.

A Goal represents a single build target within the pynixd build
orchestration system.  Goals are tracked and scheduled by the
GoalManager.

Goal handles DAG orchestration only (parents, children, dedup, result
tracking).  Execution logic is delegated to a :class:`GoalHandler`
subclass selected at construction time.
"""

from __future__ import annotations

from asyncio import Event, TaskGroup
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pynixd.types import KeyedBuildResult

from ..derived_path import DerivedPath  # noqa: TC001 — used in function bodies
from ..store_path import StorePath  # noqa: TC001 — used in dataclass fields

if TYPE_CHECKING:
    from ..store.base import Store
    from ..substitution import SubstitutionManager
    from .handler import GoalHandler
    from .manager import GoalManager


class EndGoal:
    """Controls whether a Goal tree executes or just queries."""

    BUILD = "build"
    QUERY = "query"


class GoalContext:
    """Shared context passed through the build DAG."""

    def __init__(
        self,
        goal_manager: GoalManager,
        store: Store,
        substitution_manager: SubstitutionManager,
        end_goal: str = EndGoal.BUILD,
    ) -> None:
        self.goal_manager = goal_manager
        self.store = store
        self.substitution_manager = substitution_manager
        self.end_goal = end_goal


@dataclass
class GoalResult(KeyedBuildResult):
    """Extended build result with DAG propagation metadata.

    Adds ``produced_paths`` — the set of store paths that this goal
    made available (substituted, already valid, or built).  This lets
    parents collect dependency paths without faking ``built_outputs``
    entries that are semantically about content-addressed builds.
    """

    produced_paths: set[StorePath] = field(default_factory=set)


class Goal:
    """A single build target tracked by the GoalManager.

    Each Goal wraps a ``DerivedPath`` and maintains its own dependency
    edges via ``parents`` and ``children`` sets, forming the DAG that
    the GoalManager schedules.
    """

    def __init__(
        self,
        derived_path: DerivedPath,
        ctx: GoalContext,
        handler: GoalHandler | None = None,
    ) -> None:
        self.derived_path = derived_path
        self.ctx = ctx
        self.parents: set[Goal] = set()
        self.children: set[Goal] = set()
        self.is_executing: bool = False
        self.finished_executing = Event()
        self.result: GoalResult | None = None
        self._handler = handler or _resolve_handler(derived_path)

    def add_parent(self, goal: Goal) -> None:
        self.parents.add(goal)

    def add_child(self, derived_path: DerivedPath) -> Goal:
        if goal := self.ctx.goal_manager.goals.get(derived_path):
            goal.add_parent(self)
            self.children.add(goal)
            return goal

        goal = Goal(derived_path=derived_path, ctx=self.ctx)
        goal.add_parent(self)
        self.children.add(goal)
        self.ctx.goal_manager.goals[derived_path] = goal
        return goal

    def collect_results(self) -> list[KeyedBuildResult | None]:
        results: list[KeyedBuildResult | None] = []
        results.append(self.result)
        for child in self.children:
            results.extend(child.collect_results())
        return results

    async def execute_children(self) -> None:
        async with TaskGroup() as tg:
            for child_goal in self.children:
                tg.create_task(child_goal.execute())

    async def execute(self) -> None:
        if self.is_executing:
            await self.finished_executing.wait()
            return
        self.is_executing = True

        await self._handler.execute(self)

        self.finished_executing.set()


def _resolve_handler(dp: DerivedPath) -> GoalHandler:
    """Pick the right handler based on the derived path type."""
    if dp.is_opaque:
        from .opaque import OpaqueHandler

        return OpaqueHandler()

    if dp.is_nested:
        from .dynamic import DynamicDerivationHandler

        return DynamicDerivationHandler()

    from .derivation import DerivationHandler

    return DerivationHandler()
