"""Shared build goal orchestration for the pynixd build pipeline.

The GoalManager owns the global ``GoalKey`` → ``Goal`` cache (dedup)
and provides convenience methods for executing goal trees from
``BuildPaths`` / ``QueryMissing`` request handlers.
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
    from .goal import Goal, GoalKey

log = structlog.get_logger(__name__)


class GoalManager:
    """Shared singleton that tracks and orchestrates build goals.

    All goals are indexed by ``GoalKey`` in ``self.goals``, providing
    automatic dedup across concurrent requests.
    """

    def __init__(self) -> None:
        self.goals: dict[GoalKey, Goal] = {}

    def register(self, goal: Goal) -> Goal:
        """Register *goal* in the dedup cache, or return existing.

        If a goal with the same key already exists, link *goal*'s
        parents to the existing goal and return the existing one.
        Otherwise insert *goal* and return it.
        """
        existing = self.goals.get(goal.key)
        if existing is not None:
            for p in goal.parents:
                p.add_child(existing)
            return existing
        self.goals[goal.key] = goal
        return goal

    async def build_paths(
        self,
        derived_paths: set[DerivedPath],
        store: Store,
        substitution_manager: SubstitutionManager,
    ) -> list[KeyedBuildResult]:
        """Execute a set of derived paths through the goal tree.

        Creates a fresh :class:`GoalContext` for the request, builds a goal
        for each top-level path, executes them all (goals recursively
        resolve children), and returns every result in the tree as
        :class:`KeyedBuildResult` objects.
        """
        from .goal import GoalContext, make_build_goal

        # Fresh goal cache per request — don't leak results from
        # prior QueryMissing/BuildPaths calls.
        self.goals.clear()
        ctx = GoalContext(
            goal_manager=self,
            store=store,
            substitution_manager=substitution_manager,
        )
        goals = [make_build_goal(dp, ctx) for dp in derived_paths]
        async with TaskGroup() as tg:
            for g in goals:
                tg.create_task(g.run())

        # Flatten deduplicated results
        return _flatten_as_keyed(goals)

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
        from .goal import EndGoal, GoalContext, make_build_goal

        ctx = GoalContext(
            goal_manager=self,
            store=store,
            substitution_manager=substitution_manager,
            end_goal=EndGoal.QUERY,
        )

        self.goals.clear()
        roots = [make_build_goal(dp, ctx) for dp in derived_paths]

        async with TaskGroup() as tg:
            for g in roots:
                tg.create_task(g.run())

        will_build: set[StorePath] = set()
        will_substitute: set[StorePath] = set()
        unknown: set[StorePath] = set()
        seen: set[int] = set()

        for kr in _flatten_as_keyed(roots):
            sp = kr.path.base_store_path()
            key = id(sp)
            if key in seen:
                continue
            seen.add(key)
            status = kr.result.status
            if status is BuildResultStatus.ALREADY_VALID:
                continue
            if status is BuildResultStatus.SUBSTITUTED:
                will_substitute.add(sp)
            elif status is BuildResultStatus.UNKNOWN:
                unknown.add(sp)
            else:
                will_build.add(sp)

        # Fallback: if nothing was detected, treat all root paths as
        # needing build (query mode is best-effort).
        if not will_build and not will_substitute and not unknown:
            for dp in derived_paths:
                will_build.add(dp.base_store_path())

        from ..operations.query_missing import QueryMissingResponse

        return QueryMissingResponse(
            will_build=will_build,
            will_substitute=will_substitute,
            unknown=unknown,
            download_size=0,
            nar_size=0,
        )

    if TYPE_CHECKING:
        from ..operations.query_missing import QueryMissingResponse
        from .goal import Goal, GoalContext, GoalKey, GoalResult


# ── Result flattening (GoalResult → KeyedBuildResult) ──────────────


def _flatten_as_keyed(goals: list[Goal]) -> list[KeyedBuildResult]:
    """Flatten all GoalResults into backward-compatible KeyedBuildResult list.

    Deduplicates by identity of the path object to avoid returning
    the same path from multiple goals in the tree.
    """
    seen: set[int] = set()
    results: list[KeyedBuildResult] = []

    def walk(g: Goal) -> None:
        if g.result is not None:
            key = id(g.result.path)
            if key not in seen:
                seen.add(key)
                results.append(g.result)
        for child in g.children:
            walk(child)

    for g in goals:
        walk(g)

    return results
