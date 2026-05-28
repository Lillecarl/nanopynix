"""Decomposes high-level build requests into individual derivation builds.

Three-tier execution pattern: OpRequest.handle → OpRequest.execute →
Store.execute. This module implements the decomposition logic for
BuildPaths/BuildPathsWithResults requests.

Phases:
1. Discover closure — BFS walk .drv files, expand dynamic_input_drvs
2. Enqueue and wire  — Convert to BasicDerivation, enqueue, set DAG edges
3. Await completion — Wait for all builds in the request
4. Collect results  — Synthesise KeyedBuildResult list
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import structlog

from .derived_path import DerivedPath
from .drv_parser import read_drv_file, to_basic_derivation
from .operations.base import BuildMode, KeyedBuildResult
from .operations.build_derivation import (
    BuildDerivationRequest,
)
from .operations.build_paths import BuildPathsWithResultsResponse
from .operations.query_derivation_output_map_batch import (
    QueryDerivationOutputMapBatchRequest,
)
from .operations.query_missing import QueryMissingRequest
from .operations.query_valid_paths import QueryValidPathsRequest
from .store_path import DrvOutput, StorePath
from .types.build import BuildResult, BuildResultStatus

if TYPE_CHECKING:
    from .build_queue import SchedulerBuildRequest
    from .connection import ClientConn
    from .drv_parser import Derivation
    from .scheduler import DerivationReader, Scheduler
    from .types import Realisation
    from .types.aliases import OutputMap
    from .types.ids import BuildId

log = structlog.get_logger(__name__)


def synthesize_already_valid(
    parsed: Derivation,
    status: BuildResultStatus = BuildResultStatus.ALREADY_VALID,
) -> BuildResult:
    """Build a BuildResult for an already-valid derivation.

    Constructs dummy DrvOutput hashes from parsed output paths.
    The client only uses output name and path, not the hash.
    """
    built_outputs: dict[DrvOutput, Realisation] = {}
    for out_name, out_path in parsed.output_paths().items():
        if out_path != StorePath(""):
            drv_output = DrvOutput(f"sha256:{0:064x}!{out_name}")
            built_outputs[drv_output] = {
                "id": str(drv_output),
                "outPath": out_path.name,
            }
    return BuildResult(
        status=status,
        error_msg="",
        times_built=0,
        is_non_deterministic=0,
        start_time=0,
        stop_time=0,
        cpu_user=None,
        cpu_system=None,
        built_outputs=built_outputs,
    )


class BuildDecomposer:
    """Decomposes high-level build requests into individual derivation builds.

    Request-layer component. Runs within the scope of a single BuildPaths/
    BuildPathsWithResults request. Discovers the derivation closure, submits
    builds to the scheduler's pool, awaits completion, and assembles the
    response.
    """

    read_drv_fn: DerivationReader

    def __init__(
        self,
        scheduler: Scheduler,
        read_drv_fn: DerivationReader | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.local_store = scheduler.local_store
        self.queue = scheduler.queue
        self.read_drv_fn = read_drv_fn or read_drv_file

    async def decompose(
        self,
        derived_paths: set[DerivedPath],
        build_mode: BuildMode,
        client: ClientConn | None = None,
    ) -> BuildPathsWithResultsResponse:
        """Decompose DerivedPath set into individual builds and execute them.

        Four phases:
        1. _discover_closure — BFS walk, read .drv files, expand dynamic_input_drvs
        2. _enqueue_and_wire  — Convert to BasicDerivation, enqueue, set DAG edges
        3. Await sched_req.future — wait for completion
        4. _collect_results   — Assemble KeyedBuildResult list
        """
        _req_id, sched_req = await self.queue.create_request(
            derived_paths,
            build_mode,
            client,
        )

        parsed_cache, drv_to_derived, output_cache = await self._discover_closure(derived_paths)

        drv_to_build_id = await self._enqueue_and_wire(
            parsed_cache,
            drv_to_derived,
            build_mode,
            client,
            sched_req,
            output_cache=output_cache,
        )

        if not drv_to_build_id:
            sched_req.resolve_if_done()

        result_map = await sched_req.future

        keyed_results = await self._collect_results(
            derived_paths,
            result_map,
            parsed_cache,
        )

        await self.queue.prune_request(sched_req.id)

        return BuildPathsWithResultsResponse(results=keyed_results)

    async def _build_closure_parsed_cache(
        self,
        to_build: set[StorePath],
        drv_to_derived: dict[str, DerivedPath],
    ) -> tuple[dict[StorePath, Derivation], set[StorePath]]:
        """BFS walk the derivation closure to build a parsed-cache.

        Starts from ``to_build`` and follows input_drvs and
        dynamic_input_drvs until all reachable derivations are parsed.

        For dynamic_input_drvs: if the outer derivation's outputs are
        not yet built (missing in the local store), the dyn_drv itself is
        added to the build set and recursively walked.

        Returns (parsed_cache, all_input_drvs).
        """
        parsed_cache: dict[StorePath, Derivation] = {}
        all_input_drvs: set[StorePath] = set()

        # BFS: process derivations, collecting input_drvs from each.
        # When a dynamic_input_drv has unbuilt outputs, add it to the
        # build set for recursive expansion.
        queue = list(to_build)
        visited: set[StorePath] = set()
        while queue:
            sp = queue.pop(0)
            if sp in visited:
                continue
            visited.add(sp)
            dp = drv_to_derived.get(str(sp), DerivedPath(sp))
            try:
                parsed = await dp.to_derivation(
                    self.local_store.store_path,
                    reader_fn=self.read_drv_fn,
                )
            except FileNotFoundError:
                log.warning("drv_read_failed", drv_path=dp.drv_path)
                continue

            parsed_cache[StorePath(dp.drv_path)] = parsed
            all_input_drvs.update(parsed.input_drvs.keys())

            for dyn_drv_path in parsed.dynamic_input_drvs:
                if dyn_drv_path not in to_build:
                    try:
                        dyn_parsed = await self.read_drv_fn(
                            self.local_store.store_path,
                            dyn_drv_path,
                        )
                    except FileNotFoundError:
                        continue
                    dyn_outputs = dyn_parsed.output_paths()
                    has_unbuilt = any(
                        p == StorePath("") or not self.local_store.tracker.has_path(p) for p in dyn_outputs.values()
                    )
                    if has_unbuilt:
                        to_build.add(dyn_drv_path)
                        queue.append(dyn_drv_path)
                        parsed_cache[dyn_drv_path] = dyn_parsed
                        all_input_drvs.update(dyn_parsed.input_drvs.keys())

        return parsed_cache, all_input_drvs

    async def _discover_closure(
        self,
        derived_paths: set[DerivedPath],
    ) -> tuple[dict[StorePath, Derivation], dict[str, DerivedPath], OutputMap | None]:
        """Discover the closure of derivations needed to build the requested paths.

        Returns (parsed_cache, drv_to_derived).
        Handles flattening nested paths, substitution, BFS discovery, and
        output-map queries.
        """
        flat_derived_paths: set[DerivedPath] = set()
        for dp in derived_paths:
            if isinstance(dp, DerivedPath) and dp.is_nested:
                flat_derived_paths.add(DerivedPath(dp.drv_path))
            else:
                flat_derived_paths.add(dp)

        missing_resp = await self.local_store.execute(
            QueryMissingRequest(derived_paths=flat_derived_paths),
        )

        if missing_resp.will_substitute:
            log.info("substituting_paths", count=len(missing_resp.will_substitute))
            async with self.local_store.transfer_conn() as conn:
                valid = await conn.call(
                    QueryValidPathsRequest(
                        paths=missing_resp.will_substitute,
                        substitute=1,
                    ),
                )
                self.local_store.tracker.add_known_paths(valid.paths)

        drv_to_derived: dict[str, DerivedPath] = {}
        for dp in derived_paths:
            if isinstance(dp, DerivedPath):
                drv_to_derived.setdefault(dp.drv_path, dp)

        to_build: set[StorePath] = set()
        for sp in missing_resp.will_build | missing_resp.unknown:
            to_build.add(StorePath(sp))

        parsed_cache, all_input_drvs = await self._build_closure_parsed_cache(
            to_build,
            drv_to_derived,
        )

        output_cache: OutputMap | None = None
        if all_input_drvs:
            resp = await self.local_store.execute(
                QueryDerivationOutputMapBatchRequest(drv_paths=all_input_drvs),
            )
            output_cache = resp.outputs or {}

        return parsed_cache, drv_to_derived, output_cache

    async def _convert_to_build_requests(
        self,
        parsed_cache: dict[StorePath, Derivation],
        drv_to_derived: dict[str, DerivedPath],
        build_mode: BuildMode,
        output_cache: OutputMap | None,
    ) -> tuple[list[tuple[DerivedPath, BuildDerivationRequest]], set[StorePath]]:
        """Convert parsed derivations to BasicDerivation, create build requests.

        Returns (resolved, all_input_srcs).
        """
        resolved: list[tuple[DerivedPath, BuildDerivationRequest]] = []
        all_input_srcs: set[StorePath] = set()

        for drv_path, parsed in parsed_cache.items():
            dp = drv_to_derived.get(str(drv_path), DerivedPath(drv_path))
            basic = await to_basic_derivation(
                parsed,
                self.local_store.store_path,
                output_cache=output_cache,
            )
            drv_request = BuildDerivationRequest(
                drv_path=drv_path,
                derivation=basic,
                build_mode=build_mode,
            )
            resolved.append((dp, drv_request))
            all_input_srcs.update(basic.input_srcs)

        # Validate that all input_srcs are known to the local store.
        # The scheduler method handles the internal dedup check.
        await self.scheduler.validate_known_paths(all_input_srcs)

        return resolved, all_input_srcs

    async def _enqueue_builds(
        self,
        resolved: list[tuple[DerivedPath, BuildDerivationRequest]],
        parsed_cache: dict[StorePath, Derivation],
        client: ClientConn | None,
        sched_req: SchedulerBuildRequest,
    ) -> dict[str, BuildId]:
        """Submit each build request to the scheduler.

        Returns dict[str drv_path, BuildId].
        """
        drv_to_build_id: dict[str, BuildId] = {}

        for dp, drv_request in resolved:
            drv_path_str = str(drv_request.drv_path)

            parsed = parsed_cache.get(drv_request.drv_path)
            if parsed is not None:
                drv_request.derivation.is_dynamic = parsed.is_dynamic

            required_paths: set[StorePath] = set()
            for inp in drv_request.derivation.input_srcs:
                required_paths.add(
                    StorePath(inp, extrainfo=f"input_src of {drv_path_str}"),
                )
            required_paths.add(
                StorePath(
                    drv_request.drv_path,
                    extrainfo=f"drv_path of {drv_path_str}",
                ),
            )
            build_id, _future = await self.scheduler.build_derivation(
                drv_request,
                required_paths,
                platform=drv_request.derivation.platform,
                scheduler_request_id=sched_req.id,
                derived_paths_for_request={dp},
            )
            if client is not None:
                await self.queue.subscribe(build_id, client)
            drv_to_build_id[drv_path_str] = build_id
            log.info(
                "build_derivation_enqueued",
                build_id=build_id,
                drv_path=drv_request.drv_path,
            )

        return drv_to_build_id

    async def _wire_dag_edges(
        self,
        resolved: list[tuple[DerivedPath, BuildDerivationRequest]],
        parsed_cache: dict[StorePath, Derivation],
        drv_to_build_id: dict[str, BuildId],
    ) -> None:
        """Set DAG edges between builds from input_drvs and dynamic_input_drvs."""
        for _dp, drv_request in resolved:
            drv_path_str = str(drv_request.drv_path)
            parsed = parsed_cache.get(drv_request.drv_path)
            if parsed is None:
                continue

            depends_on: set[BuildId] = set()
            for input_drv in parsed.input_drvs:
                dep_id = drv_to_build_id.get(str(input_drv))
                if dep_id is not None and dep_id != drv_to_build_id.get(drv_path_str):
                    depends_on.add(dep_id)

            for dyn_drv in parsed.dynamic_input_drvs:
                dep_id = drv_to_build_id.get(str(dyn_drv))
                if dep_id is not None and dep_id != drv_to_build_id.get(drv_path_str):
                    depends_on.add(dep_id)

            build_id = drv_to_build_id.get(drv_path_str)
            if build_id is not None and parsed.dynamic_input_drvs:
                build = self.queue.by_id.get(build_id)
                if build is not None:
                    build.dynamic_input_drvs = parsed.dynamic_input_drvs

            if depends_on:
                build_id = drv_to_build_id[drv_path_str]
                await self.queue.set_depends_on(build_id, depends_on)

    async def _enqueue_and_wire(
        self,
        parsed_cache: dict[StorePath, Derivation],
        drv_to_derived: dict[str, DerivedPath],
        build_mode: BuildMode,
        client: ClientConn | None,
        sched_req: SchedulerBuildRequest,
        output_cache: OutputMap | None = None,
    ) -> dict[str, BuildId]:
        """Convert parsed derivations to BasicDerivation, enqueue, set DAG edges."""
        resolved, _ = await self._convert_to_build_requests(
            parsed_cache,
            drv_to_derived,
            build_mode,
            output_cache,
        )

        drv_to_build_id = await self._enqueue_builds(
            resolved,
            parsed_cache,
            client,
            sched_req,
        )

        await self._wire_dag_edges(resolved, parsed_cache, drv_to_build_id)

        return drv_to_build_id

    async def _resolve_already_valid(
        self,
        dp: DerivedPath,
        parsed_cache: dict[StorePath, Derivation],
    ) -> Derivation | None:
        """Find a parsed derivation for dp, falling back to a disk read."""
        drv_store_path = StorePath(dp.drv_path)
        if not drv_store_path.is_derivation():
            return None
        parsed = parsed_cache.get(drv_store_path)
        if parsed is None:
            with contextlib.suppress(FileNotFoundError):
                parsed = await dp.to_derivation(
                    self.local_store.store_path,
                    reader_fn=self.read_drv_fn,
                )
        return parsed

    async def _collect_results(
        self,
        derived_paths: set[DerivedPath],
        result_map: dict[DerivedPath, BuildResult],
        parsed_cache: dict[StorePath, Derivation],
    ) -> list[KeyedBuildResult]:
        """Assemble KeyedBuildResult list from completed builds.

        Synthesises ALREADY_VALID results for derivations that were already
        cached and never needed to be built.
        """
        keyed_results: list[KeyedBuildResult] = []
        for dp in derived_paths:
            if not isinstance(dp, DerivedPath):
                continue
            br = result_map.get(dp)

            if br is None:
                parsed = await self._resolve_already_valid(dp, parsed_cache)
                if parsed is not None:
                    br = synthesize_already_valid(parsed)
                else:
                    br = BuildResult(
                        status=BuildResultStatus.ALREADY_VALID,
                        error_msg="",
                        times_built=0,
                        is_non_deterministic=0,
                        start_time=0,
                        stop_time=0,
                        built_outputs={},
                    )
            elif br.status == BuildResultStatus.ALREADY_VALID and not br.built_outputs:
                parsed = await self._resolve_already_valid(dp, parsed_cache)
                if parsed is not None:
                    synthesized = synthesize_already_valid(parsed)
                    br = BuildResult(
                        status=br.status,
                        error_msg=br.error_msg,
                        times_built=br.times_built,
                        is_non_deterministic=br.is_non_deterministic,
                        start_time=br.start_time,
                        stop_time=br.stop_time,
                        built_outputs=synthesized.built_outputs,
                        cpu_user=br.cpu_user,
                        cpu_system=br.cpu_system,
                    )

            keyed_results.append(KeyedBuildResult(path=dp, result=br))
        return keyed_results
