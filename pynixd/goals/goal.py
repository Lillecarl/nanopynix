"""Build goal representation.

A Goal represents a single build target within the pynixd build
orchestration system.  Goals are tracked and scheduled by the
GoalManager.

Two core goal kinds:

* **BuildGoal** — ensure a DerivedPath exists (check, substitute, build).
* **ResolutionGoal** — compute the output path of a single derivation
  output via the unparsing math (ATerm → hash → path).

Each goal is identified by a ``GoalKey`` in the ``GoalManager`` for
deduplication across concurrent requests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from asyncio import Event, TaskGroup
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from pynixd.types import KeyedBuildResult

from ..derived_path import DerivedPath  # noqa: TC001 — used in function bodies
from ..store_path import StorePath  # noqa: TC001 — used in dataclass fields

log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from ..store.base import Store
    from ..substitution import SubstitutionManager
    from .manager import GoalManager


# ── Goal key for dedup ────────────────────────────────────────────


@dataclass(frozen=True)
class GoalKey:
    """Unique identifier for a Goal in the GoalManager.

    Three tag families:
    * ``"build"`` — a BuildGoal ensuring a DerivedPath exists.
    * ``"resolve"`` — a ResolutionGoal computing one output of a
      derivation via hashDerivationModulo.
    * ``"substitute"`` — a SubstitutionGoal fetching a store path
      from a binary cache.
    """

    tag: str  # "build" | "resolve" | "substitute"
    path: str  # normalized store path string
    output: str  # output name ("" for opaque/substitute paths)

    @classmethod
    def build(cls, dp: DerivedPath) -> GoalKey:
        """Key for a BuildGoal that ensures *dp* exists."""
        return cls(tag="build", path=str(dp.base_store_path()), output=_dp_output(dp))

    @classmethod
    def resolve(cls, drv_path: StorePath, output_name: str) -> GoalKey:
        """Key for a ResolutionGoal that computes one output path."""
        return cls(tag="resolve", path=str(drv_path), output=output_name)

    @classmethod
    def substitute(cls, path: StorePath) -> GoalKey:
        """Key for a SubstitutionGoal that fetches a store path."""
        return cls(tag="substitute", path=str(path), output="")


def _dp_output(dp: DerivedPath) -> str:
    """Extract the canonical output identifier from a DerivedPath.

    For nested paths (e.g. ``a.drv!out!lib``), the chain is encoded
    as ``out!lib`` so that the GoalKey is distinct from the flat
    ``a.drv!lib`` goal key.
    """
    if dp.is_opaque:
        return ""
    suffix = "!".join(dp.chain)
    names = dp.output_names
    if len(names) == 1:
        out = next(iter(names))
    else:
        out = "*"
    return f"{suffix}!{out}" if suffix else out


# ── End goal mode ────────────────────────────────────────────────


class EndGoal:
    BUILD = "build"
    QUERY = "query"


# ── Shared context ────────────────────────────────────────────────


class GoalContext:
    """Shared context passed through the goal DAG."""

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


# ── Goal result ────────────────────────────────────────────────────


@dataclass
class GoalResult(KeyedBuildResult):
    """Extended build result with DAG propagation and resolution metadata.

    Adds ``produced_paths`` — the set of store paths that this goal
    made available (substituted, already valid, or built).  This lets
    parents collect dependency paths without faking ``built_outputs``
    entries that are semantically about content-addressed builds.

    Adds ``resolved_outputs`` — output name → resolved store path,
    set by resolution goals and consumed by parent build goals for
    placeholder rewriting.

    Adds ``modulo_hash`` — hex SHA256 of the hashDerivationModulo
    for this derivation.  Needed by parent resolution goals to
    compute their own hashDerivationModulo.
    """

    produced_paths: set[StorePath] = field(default_factory=set)
    resolved_outputs: dict[str, StorePath] = field(default_factory=dict)
    modulo_hash: str = ""


# ── Abstract Goal base ─────────────────────────────────────────────


class Goal(ABC):
    """A single build target tracked by the GoalManager.

    Each goal is uniquely identified by :attr:`key` in the manager's
    dedup index.  Subclasses implement :meth:`execute` with their
    specific logic and set :attr:`result` when done.

    DAG helpers (``add_child``, ``execute_children``, ``collect_results``)
    are shared by all subclasses.
    """

    def __init__(self, ctx: GoalContext) -> None:
        self.ctx = ctx
        self.parents: set[Goal] = set()
        self.children: set[Goal] = set()
        self.is_executing: bool = False
        self.finished_executing = Event()
        self.result: GoalResult | None = None

    # ── subclasses must define ────────────────────────────────────

    @property
    @abstractmethod
    def key(self) -> GoalKey:
        """Key for dedup in the GoalManager."""

    @abstractmethod
    async def execute(self) -> None:
        """Execute this goal and set ``self.result``."""

    # ── DAG helpers ───────────────────────────────────────────────

    def add_parent(self, goal: Goal) -> None:
        self.parents.add(goal)

    def add_child(self, child: Goal) -> None:
        """Register *child* as a dependency — it will execute before us."""
        child.add_parent(self)
        self.children.add(child)

    async def execute_children(self) -> None:
        """Execute all children in parallel (dedup via ``run()``)."""
        async with TaskGroup() as tg:
            for child in self.children:
                tg.create_task(child.run())

    def collect_results(self) -> list[GoalResult | None]:
        """Depth-first collection of all results in the subtree."""
        results: list[GoalResult | None] = [self.result]
        for child in self.children:
            results.extend(child.collect_results())
        return results

    # ── Execution lifecycle ───────────────────────────────────────

    async def run(self) -> None:
        """Run this goal (with dedup: if already executing, wait)."""
        if self.is_executing:
            await self.finished_executing.wait()
            return
        self.is_executing = True
        try:
            await self.execute()
        finally:
            self.finished_executing.set()


# ── Factory helpers ────────────────────────────────────────────────


def make_build_goal(dp: DerivedPath, ctx: GoalContext) -> Goal:
    """Create the right BuildGoal subclass for a DerivedPath."""
    if dp.is_opaque:
        from .opaque import OpaqueBuildGoal

        return OpaqueBuildGoal(derived_path=dp, ctx=ctx)

    if dp.is_nested:
        from .dynamic import DynamicBuildGoal

        return DynamicBuildGoal(derived_path=dp, ctx=ctx)

    from .derivation import DerivationBuildGoal

    return DerivationBuildGoal(derived_path=dp, ctx=ctx)


def make_resolution_goal(
    drv_path: StorePath,
    output_name: str,
    ctx: GoalContext,
) -> Goal:
    """Create a ResolutionGoal for one output of a derivation."""
    from .resolution import ResolutionGoal

    return ResolutionGoal(drv_path=drv_path, output_name=output_name, ctx=ctx)
