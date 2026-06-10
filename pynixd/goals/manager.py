"""Shared build goal orchestration for the pynixd build pipeline.

The GoalManager owns per-type dedup maps and provides methods for
executing goal trees from ``BuildPaths`` / ``QueryMissing`` request handlers.
"""

from __future__ import annotations

from asyncio import TaskGroup
from typing import TYPE_CHECKING

import structlog

from ..derived_path import DerivedPath  # noqa: TC001 — used in function bodies
from ..store_path import StorePath  # noqa: TC001 — used in function bodies
from ..types.build import BuildResultStatus, KeyedBuildResult

if TYPE_CHECKING:
    from ..drv_parser import DrvOutput
    from ..operations.query_missing import QueryMissingResponse
    from ..store.base import Store
    from ..substitution import SubstitutionManager
    from .goal import Goal, GoalContext

log = structlog.get_logger(__name__)


class GoalManager:
    """Per-type dedup maps for build goals.

    Each goal type has its own index, keyed by the appropriate target type:
    - Trampoline goals: DerivedPath
    - Path substitution goals: StorePath
    - Drv output substitution goals: DrvOutput
    - Derivation building goals: StorePath
    """

    def __init__(self) -> None:
        self._trampoline_goals: dict[DerivedPath, Goal] = {}
        self._path_substitution_goals: dict[StorePath, Goal] = {}
        self._drv_output_sub_goals: dict[DrvOutput, Goal] = {}
        self._derivation_building_goals: dict[StorePath, Goal] = {}

    # ── Per-type get_or_create ─────────────────────────────────────

    def get_or_create_trampoline(
        self,
        dp: DerivedPath,
        ctx: GoalContext,
    ) -> Goal:
        existing = self._trampoline_goals.get(dp)
        if existing:
            return existing
        from .trampoline import DerivationTrampolineGoal

        g = DerivationTrampolineGoal(dp, ctx)
        self._trampoline_goals[dp] = g
        return g

    def get_or_create_path_substitution(
        self,
        path: StorePath,
        ctx: GoalContext,
    ) -> Goal:
        existing = self._path_substitution_goals.get(path)
        if existing:
            return existing
        from ..derived_path import DerivedPath
        from .path_substitution import PathSubstitutionGoal

        g = PathSubstitutionGoal(
            derived_path=DerivedPath._from_components(
                drv_path=path,
                chain=(),
                outputs=None,
            ),
            ctx=ctx,
        )
        self._path_substitution_goals[path] = g
        return g

    def get_or_create_drv_output_sub(
        self,
        drv_output: DrvOutput,
        ctx: GoalContext,
    ) -> Goal:
        existing = self._drv_output_sub_goals.get(drv_output)
        if existing:
            return existing
        from .drv_output_substitution import DrvOutputSubstitutionGoal

        g = DrvOutputSubstitutionGoal(drv_output, ctx)
        self._drv_output_sub_goals[drv_output] = g
        return g

    def get_or_create_derivation_building(
        self,
        drv_path: StorePath,
        ctx: GoalContext,
    ) -> Goal:
        existing = self._derivation_building_goals.get(drv_path)
        if existing:
            return existing
        from .building import DerivationBuildingGoal

        g = DerivationBuildingGoal(drv_path, ctx)
        self._derivation_building_goals[drv_path] = g
        return g

    # ── Compatibility register ─────────────────────────────────────

    def register(self, goal: Goal) -> Goal:
        """Register *goal* in the appropriate per-type map, or return existing.

        Dispatches to the right ``get_or_create_*`` based on goal type.
        For goal types not managed by the index (e.g. ResolutionGoal,
        DerivationGoal), returns *goal* directly.
        """
        from .building import DerivationBuildingGoal
        from .drv_output_substitution import DrvOutputSubstitutionGoal
        from .path_substitution import PathSubstitutionGoal
        from .trampoline import DerivationTrampolineGoal

        if isinstance(goal, DerivationTrampolineGoal):
            return self.get_or_create_trampoline(goal.derived_path, goal.ctx)
        if isinstance(goal, PathSubstitutionGoal):
            sp = goal._derived_path.base_store_path()
            return self.get_or_create_path_substitution(sp, goal.ctx)
        if isinstance(goal, DrvOutputSubstitutionGoal):
            return self.get_or_create_drv_output_sub(goal.drv_output, goal.ctx)
        if isinstance(goal, DerivationBuildingGoal):
            return self.get_or_create_derivation_building(goal.drv_path, goal.ctx)
        # Unindexed goal types (ResolutionGoal, DerivationGoal)
        return goal

    # ── build_paths ────────────────────────────────────────────────

    async def build_paths(
        self,
        derived_paths: set[DerivedPath],
        store: Store,
        substitution_manager: SubstitutionManager,
        scheduler: object | None = None,
    ) -> list[KeyedBuildResult]:
        """Execute a set of derived paths through the goal tree.

        Creates fresh per-type maps for the request, builds a trampoline
        goal for each top-level path, executes them all, and returns
        results via ``_collect_results``.
        """
        from .goal import GoalContext

        self._clear()
        ctx = GoalContext(
            goal_manager=self,
            store=store,
            substitution_manager=substitution_manager,
            scheduler=scheduler,
        )
        roots = [self.get_or_create_trampoline(dp, ctx) for dp in derived_paths]
        async with TaskGroup() as tg:
            for root in roots:
                tg.create_task(root.run())
        return _collect_results(roots)

    async def query_paths(
        self,
        derived_paths: set[DerivedPath],
        store: Store,
        substitution_manager: SubstitutionManager,
        scheduler: object | None = None,
    ) -> QueryMissingResponse:
        """Determine which paths need building, substitution, or are unknown."""
        from .goal import EndGoal, GoalContext

        ctx = GoalContext(
            goal_manager=self,
            store=store,
            substitution_manager=substitution_manager,
            end_goal=EndGoal.QUERY,
            scheduler=scheduler,
        )
        self._clear()
        roots = [self.get_or_create_trampoline(dp, ctx) for dp in derived_paths]
        async with TaskGroup() as tg:
            for root in roots:
                tg.create_task(root.run())

        will_build: set[StorePath] = set()
        will_substitute: set[StorePath] = set()
        unknown: set[StorePath] = set()
        seen: set[int] = set()

        for kr in _collect_results(roots):
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

    # ── Internal ───────────────────────────────────────────────────

    def _clear(self) -> None:
        """Clear all per-type maps."""
        self._trampoline_goals.clear()
        self._path_substitution_goals.clear()
        self._drv_output_sub_goals.clear()
        self._derivation_building_goals.clear()


# ── Result collection ──────────────────────────────────────────────


def _collect_results(roots: list) -> list[KeyedBuildResult]:
    """Collect results from root goals.

    Successful roots → append their result.
    Failed roots → walk children to find first failure.
    """
    results: list[KeyedBuildResult] = []
    for root in roots:
        if root.result and root.result.result.status.is_success:
            results.append(root.result)
        else:
            failed = _find_failure(root)
            if failed:
                results.append(failed)
    return results


def _find_failure(g) -> KeyedBuildResult | None:
    """Walk tree to find first goal with a failure status."""
    if g.result and g.result.result.status.is_failure:
        return g.result
    for child in getattr(g, "children", []):
        found = _find_failure(child)
        if found:
            return found
    return None
