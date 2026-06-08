"""Shared build goal orchestration for the pynixd build pipeline.

The GoalManager is a singleton that owns the global ``DerivedPath`` → ``Goal``
cache (dedup) and provides convenience methods for executing goal trees
from ``BuildPaths`` / ``QueryMissing`` request handlers.
"""

from __future__ import annotations

from asyncio import TaskGroup
from typing import TYPE_CHECKING

import structlog

from ..derived_path import DerivedPath  # noqa: TC001 — used in function bodies
from ..store_path import StorePath  # noqa: TC001 — used in function bodies
from ..types.build import BuildResultStatus, KeyedBuildResult

if TYPE_CHECKING:
    from ..store.base import Store
    from ..substitution import SubstitutionManager
    from .goal import Goal

log = structlog.get_logger(__name__)


class GoalManager:
    """Shared singleton that tracks and orchestrates build goals.

    All goals are indexed by ``DerivedPath`` in ``self.goals``, providing
    automatic dedup across concurrent requests.

    Handlers should call :meth:`build_paths` or :meth:`query_paths` rather
    than constructing goals directly.
    """

    def __init__(self) -> None:
        self.goals: dict[DerivedPath, Goal] = {}

    async def build_paths(
        self,
        derived_paths: set[DerivedPath],
        store: Store,
        substitution_manager: SubstitutionManager,
    ) -> list[KeyedBuildResult]:
        """Execute a set of derived paths through the goal tree.

        Creates a fresh :class:`GoalContext` for the request, builds a goal
        for each top-level path, executes them all (the handlers recursively
        resolve children), and returns every result in the tree.
        """
        from .goal import Goal

        # Fresh goal cache per request — don't leak results from
        # prior QueryMissing/BuildPaths calls.
        self.goals.clear()
        ctx = self._make_ctx(store, substitution_manager)
        goals = [Goal(derived_path=dp, ctx=ctx) for dp in derived_paths]
        async with TaskGroup() as tg:
            for g in goals:
                tg.create_task(g.execute())

        results: list[KeyedBuildResult] = []
        seen: set[int] = set()
        for g in goals:
            for r in g.collect_results():
                if r is None:
                    continue
                key = id(r.path)
                if key in seen:
                    continue
                seen.add(key)
                results.append(r)
        return results

    async def query_paths(
        self,
        derived_paths: set[DerivedPath],
        store: Store,
        substitution_manager: SubstitutionManager,
    ) -> QueryMissingResponse:
        """Determine which paths need building, substitution, or are unknown.

        Runs the goal tree in ``QUERY`` mode so no expensive operations
        (builds, actual substitutions) execute.  The result mirrors Nix's
        ``QueryMissing`` response.
        """
        from .goal import EndGoal, Goal, GoalContext

        ctx = GoalContext(
            goal_manager=self,
            store=store,
            substitution_manager=substitution_manager,
            end_goal=EndGoal.QUERY,
        )

        # Fresh goal cache per request — don't leak results from
        # prior build requests into the query.
        self.goals.clear()
        roots: list[Goal] = []
        for dp in derived_paths:
            if dp not in self.goals:
                self.goals[dp] = Goal(derived_path=dp, ctx=ctx)
            roots.append(self.goals[dp])

        async with TaskGroup() as tg:
            for g in roots:
                tg.create_task(g.execute())

        will_build: set[StorePath] = set()
        will_substitute: set[StorePath] = set()
        unknown: set[StorePath] = set()
        seen: set[int] = set()

        for g in roots:
            for r in g.collect_results():
                if r is None:
                    continue
                path = r.path.base_store_path()
                key = id(path)
                if key in seen:
                    continue
                seen.add(key)
                status = r.result.status
                if status is BuildResultStatus.ALREADY_VALID:
                    continue
                if status is BuildResultStatus.SUBSTITUTED:
                    will_substitute.add(path)
                elif status is BuildResultStatus.UNKNOWN:
                    unknown.add(path)
                else:
                    will_build.add(path)

        from ..operations.query_missing import QueryMissingResponse

        return QueryMissingResponse(
            will_build=will_build,
            will_substitute=will_substitute,
            unknown=unknown,
            download_size=0,
            nar_size=0,
        )

    def _make_ctx(
        self,
        store: Store,
        substitution_manager: SubstitutionManager,
    ) -> GoalContext:
        from .goal import EndGoal, GoalContext

        return GoalContext(
            goal_manager=self,
            store=store,
            substitution_manager=substitution_manager,
            end_goal=EndGoal.BUILD,
        )

    if TYPE_CHECKING:
        from ..operations.query_missing import QueryMissingResponse
        from ..types.build import KeyedBuildResult
        from .goal import Goal, GoalContext
