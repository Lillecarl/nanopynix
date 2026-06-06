"""Build goal representation.

A Goal represents a single build target within the pynixd build
orchestration system. Goals are tracked and scheduled by the GoalManager.
"""

from __future__ import annotations

from asyncio import Event, TaskGroup
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from pynixd.operations.build_derivation import BuildDerivationRequest
from pynixd.operations.ca_derivations import RegisterDrvOutputRequest
from pynixd.operations.is_valid_path import IsValidPathRequest
from pynixd.types import BasicDerivation, BuildMode, DerivationOutput, KeyedBuildResult
from pynixd.types.build import BuildResult, BuildResultStatus

from ..derived_path import DerivedPath
from ..drv_parser import read_drv_file
from ..store_path import StorePath

log = structlog.get_logger()

if TYPE_CHECKING:
    from ..drv_parser import Derivation
    from ..store.base import Store
    from ..substitution import SubstitutionManager
    from .manager import GoalManager


class GoalContext:
    """Shared context passed through the build DAG."""

    def __init__(self, goal_manager: GoalManager, store: Store, substitution_manager: SubstitutionManager) -> None:
        self.goal_manager = goal_manager
        self.store = store
        self.substitution_manager = substitution_manager


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

    def __init__(self, derived_path: DerivedPath, ctx: GoalContext) -> None:
        self.derived_path = derived_path
        self.ctx = ctx
        self.parents: set[Goal] = set()
        self.children: set[Goal] = set()
        self.is_executing: bool = False
        self.finished_executing = Event()
        self.result: GoalResult | None = None

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

    async def execute_children(self):
        async with TaskGroup() as tg:
            for child_goal in self.children:
                tg.create_task(child_goal.execute())

    async def execute_derivation(self, derivation: Derivation) -> None:
        assert not self.derived_path.is_opaque
        log.info(
            "execute_derivation",
            derived_path=self.derived_path.derived,
        )

        derived_outputs: dict[DerivedPath, DerivationOutput] = {}

        for output in derivation.outputs:
            derived_path = DerivedPath(f"{self.derived_path.base_store_path()}!{output.name}")
            derivation_output = DerivationOutput(
                path=output.path, method=output.hash_algo, hash_digest=output.hash_value
            )
            derived_outputs[derived_path] = derivation_output

        output = derived_outputs[self.derived_path]

        if (await self.ctx.store.execute(IsValidPathRequest(path=StorePath(output.path)))).valid:
            self.result = GoalResult(
                path=self.derived_path,
                result=BuildResult(status=BuildResultStatus.ALREADY_VALID),
                produced_paths={StorePath(output.path)},
            )
            return

        log.info("checking_substituters", path=output.path)
        if await self.ctx.substitution_manager.check(StorePath(output.path)):
            goal = self.add_child(DerivedPath(output.path))
            await self.execute_children()
            if goal.result:
                self.result = goal.result
                self.result.path = self.derived_path
            return

        for path, outputs in derivation.input_drvs.items():
            for output in outputs:
                self.add_child(DerivedPath(f"{path}!{output}"))
        for path in derivation.input_srcs:
            self.add_child(DerivedPath(path))

        await self.execute_children()

        input_srcs: set[StorePath] = set()
        for result in self.collect_results():
            if not isinstance(result, KeyedBuildResult):
                continue
            if isinstance(result, GoalResult):
                input_srcs.update(result.produced_paths)
            input_srcs.update(StorePath(output["outPath"]) for output in result.result.built_outputs.values())

        log.info("building", derivation=self.derived_path.drv_path, input_srcs=input_srcs)
        response = await self.ctx.store.execute(
            BuildDerivationRequest(
                drv_path=self.derived_path.base_store_path(),
                derivation=BasicDerivation(
                    outputs={
                        o.name: DerivationOutput(
                            path=o.path,
                            method=o.hash_algo,
                            hash_digest=o.hash_value,
                        )
                        for o in derivation.outputs
                    },
                    input_srcs=input_srcs,
                    args=derivation.args,
                    builder=derivation.builder,
                    env=derivation.env,
                    is_dynamic=derivation.is_dynamic,
                    platform=derivation.platform,
                ),
                build_mode=BuildMode.NORMAL,
            )
        )
        self.result = GoalResult(
            path=self.derived_path,
            result=response.result,
            produced_paths={StorePath(o.path) for o in derivation.outputs if o.path},
        )
        for realisation in self.result.result.built_outputs.values():
            await self.ctx.store.execute(RegisterDrvOutputRequest(realisation=realisation))

    async def execute_opaque(self):
        assert self.derived_path.is_opaque
        log.info(
            "execute_opaque",
            derived_path=self.derived_path.derived,
        )

        if (await self.ctx.store.execute(IsValidPathRequest(path=self.derived_path.base_store_path()))).valid:
            self.result = GoalResult(
                path=self.derived_path,
                result=BuildResult(status=BuildResultStatus.ALREADY_VALID),
                produced_paths={self.derived_path.base_store_path()},
            )
            return

        if info := await self.ctx.substitution_manager.check(self.derived_path.base_store_path()):
            for path in info.references:
                if self.derived_path.base_store_path() == path:
                    continue  # don't create a child for itself
                self.add_child(DerivedPath(path))

            await self.execute_children()

            log.info("substituting", path=self.derived_path.base_store_path())
            await self.ctx.substitution_manager.substitute({self.derived_path.base_store_path()}, self.ctx.store)
            self.result = GoalResult(
                path=self.derived_path,
                result=BuildResult(status=BuildResultStatus.SUBSTITUTED),
                produced_paths={self.derived_path.base_store_path()},
            )
        else:
            self.result = GoalResult(
                path=self.derived_path, result=BuildResult(status=BuildResultStatus.NO_SUBSTITUTERS)
            )

    async def execute(self) -> None:
        if self.is_executing:
            await self.finished_executing.wait()
            return
        self.is_executing = True

        if self.derived_path.is_opaque:
            await self.execute_opaque()
        elif derivation := await read_drv_file(self.ctx.store.store_path, self.derived_path.base_store_path()):
            await self.execute_derivation(derivation)

        self.finished_executing.set()
