"""Goal registry and daemon request entrypoints."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import anyio

from ..serde import BuildDerivationRequest, BuildPathsRequest, BuildPathsResponse, BuildPathsWithResultsRequest
from .build_derivation import BuildDerivationGoal
from .ensure import EnsureDerivedPathGoal
from .goal import Goal
from .keys import BuildDerivationKey, EnsureDerivedPathKey, SubstitutePathKey
from .requests import BuildPathsWithResultsGoal
from .results import result_succeeded
from .substitute import SubstitutePathGoal, substituter_fingerprint

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..context import PynixdContext
    from ..derived_path import DerivedPath
    from ..serde.ids import BuildId
    from ..store_path import StorePath


def _derivation_fingerprint(request: BuildDerivationRequest) -> str:
    payload = request.derivation.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class GoalEngine:
    """Global active-goal registry and request entrypoint."""

    def __init__(self, ctx: PynixdContext) -> None:
        self.ctx = ctx
        self._lock = anyio.Lock()
        self._goals: dict[Any, Goal[Any]] = {}
        self.substitution_import_limiter = anyio.Semaphore(4)

    async def subscribe_build(self, build_id: BuildId, client: ClientConn) -> None:
        scheduler = self.ctx.scheduler
        if scheduler is None:
            return
        await scheduler.queue.subscribe(build_id, client)

    async def build_paths(self, request: BuildPathsRequest, client: ClientConn | None = None) -> BuildPathsResponse:
        response = await self.build_paths_with_results(
            BuildPathsWithResultsRequest(
                derived_paths=request.derived_paths,
                build_mode=request.build_mode,
            ),
            client=client,
        )
        return BuildPathsResponse(value=0 if all(result_succeeded(item.result) for item in response.results) else 1)

    async def build_paths_with_results(
        self,
        request: BuildPathsWithResultsRequest,
        client: ClientConn | None = None,
    ):
        return await BuildPathsWithResultsGoal(self, request, client).result()

    async def get_ensure_derived_path_goal(
        self,
        path: DerivedPath,
        build_mode: int,
        substituter_urls: tuple[str, ...],
    ) -> EnsureDerivedPathGoal:
        key = EnsureDerivedPathKey(str(path), substituter_fingerprint(substituter_urls))
        async with self._lock:
            goal = self._goals.get(key)
            if goal is None:
                goal = EnsureDerivedPathGoal(self, path, build_mode, substituter_urls)
                self._goals[key] = goal
            if not isinstance(goal, EnsureDerivedPathGoal):
                raise RuntimeError(f"goal key collision for {key}")
            return goal

    async def get_build_derivation_goal(self, request: BuildDerivationRequest) -> BuildDerivationGoal:
        key = BuildDerivationKey(str(request.drv_path), _derivation_fingerprint(request))
        async with self._lock:
            goal = self._goals.get(key)
            if goal is None:
                goal = BuildDerivationGoal(self, request)
                self._goals[key] = goal
            if not isinstance(goal, BuildDerivationGoal):
                raise RuntimeError(f"goal key collision for {key}")
            return goal

    async def get_substitute_path_goal(
        self,
        path: StorePath,
        substituter_urls: tuple[str, ...],
    ) -> SubstitutePathGoal:
        key = SubstitutePathKey(str(path), substituter_fingerprint(substituter_urls))
        async with self._lock:
            goal = self._goals.get(key)
            if goal is None:
                goal = SubstitutePathGoal(self, path, substituter_urls)
                self._goals[key] = goal
            if not isinstance(goal, SubstitutePathGoal):
                raise RuntimeError(f"goal key collision for {key}")
            return goal
