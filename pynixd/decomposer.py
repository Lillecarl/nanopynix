from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from .derived_path import DerivedPath
from .drv_parser import read_drv_file, to_basic_derivation
from .operations.base import BuildMode, KeyedBuildResult
from .operations.build_derivation import (
    BuildDerivationRequest,
)
from .operations.build_paths import BuildPathsWithResultsResponse
from .operations.query_derivation_outputs_batch import (
    QueryDerivationOutputsBatchRequest,
)
from .operations.query_missing import QueryMissingRequest
from .operations.query_valid_paths import QueryValidPathsRequest
from .store_path import StorePath

if TYPE_CHECKING:
    from .connection import ClientConn
    from .drv_parser import ParsedDerivation
    from .scheduler import DerivationReader, Scheduler

log = structlog.get_logger(__name__)


class BuildDecomposer:
    """Decomposes high-level build requests into individual derivation builds."""

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

        Handles: substitution, .drv parsing, DAG linking, enqueueing.
        Returns a BuildPathsWithResultsResponse — callers that don't need
        per-key results (BuildPaths) can just check for failures.
        """
        _req_id, sched_req = await self.queue.create_request(
            derived_paths,
            build_mode,
            client,
        )

        # For nested DerivedPaths (e.g., a.drv^out^out), QueryMissing
        # doesn't understand them. Extract the outermost .drv path for
        # the initial build phase — the nested chain will be handled by
        # the trampoline.
        flat_derived_paths: set[DerivedPath] = set()
        for dp in derived_paths:
            if isinstance(dp, DerivedPath) and dp.is_nested:
                outer_dp = DerivedPath(dp.drv_path)
                flat_derived_paths.add(outer_dp)
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

        parsed_cache: dict[StorePath, ParsedDerivation] = {}
        all_planned_outputs: set[StorePath] = set()
        all_input_drvs: set[StorePath] = set()

        # Collect all derivations that need to be built, including
        # those referenced via dynamic_input_drvs.
        to_build: set[StorePath] = set()
        for sp in missing_resp.will_build | missing_resp.unknown:
            to_build.add(StorePath(sp))

        # Expand to_build with dynamic_input_drvs targets.
        # A wrapper's dynamic deps (producingDrv) may not appear in
        # will_build because the .drv file is a valid path but its
        # outputs haven't been built yet.
        queue = list(to_build)
        visited: set[StorePath] = set()
        while queue:
            sp = queue.pop(0)
            if sp in visited:
                continue
            visited.add(sp)
            dp = drv_to_derived.get(str(sp), DerivedPath(sp))
            try:
                parsed = dp.to_derivation(
                    self.local_store.store_path,
                    reader_fn=self.read_drv_fn,
                )
            except FileNotFoundError:
                log.warning("drv_read_failed", drv_path=dp.drv_path)
                continue

            parsed_cache[StorePath(dp.drv_path)] = parsed
            for p in parsed.output_paths().values():
                if p != StorePath(""):
                    all_planned_outputs.add(p)
            all_input_drvs.update(parsed.input_drvs.keys())

            # Add dynamic_input_drvs targets to the build queue.
            # These are derivations whose outputs are needed but may
            # not be in will_build (their .drv files are valid paths
            # but their outputs aren't built yet).
            for dyn_drv_path in parsed.dynamic_input_drvs:
                if dyn_drv_path not in to_build:
                    # Check if this dynamic dep's outputs are already available
                    try:
                        dyn_parsed = self.read_drv_fn(
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

        output_cache = None
        if all_input_drvs:
            resp = await self.local_store.execute(
                QueryDerivationOutputsBatchRequest(drv_paths=all_input_drvs),
            )
            output_cache = resp.outputs if resp.outputs else {}

        resolved: list[tuple[DerivedPath, set[str], BuildDerivationRequest]] = []
        all_input_srcs: set[StorePath] = set()

        for sp in to_build:
            dp = drv_to_derived.get(str(sp), DerivedPath(sp))
            drv_path = StorePath(dp.drv_path)
            parsed = parsed_cache.get(drv_path)
            if parsed is None:
                continue

            basic = to_basic_derivation(
                parsed,
                self.local_store.store_path,
                output_cache=output_cache,
            )
            drv_request = BuildDerivationRequest(
                drv_path=drv_path,
                derivation=basic,
                build_mode=build_mode,
            )
            resolved.append((dp, dp.output_names, drv_request))
            all_input_srcs.update(basic.input_srcs)

        unknown = all_input_srcs - self.local_store.tracker.known_paths
        if unknown:
            valid_resp = await self.local_store.execute(
                QueryValidPathsRequest(paths=unknown),
            )
            self.local_store.tracker.add_known_paths(
                valid_resp.paths,
                update_regtime=False,
            )

        drv_to_build_id: dict[str, int] = {}

        for _dp, _output_names, drv_request in resolved:
            if drv_request.drv_path in parsed_cache:
                drv_request.derivation.is_dynamic = parsed_cache[drv_request.drv_path].is_dynamic
            else:
                try:
                    parsed = self.read_drv_fn(
                        self.local_store.store_path,
                        drv_request.drv_path,
                    )
                    drv_request.derivation.is_dynamic = parsed.is_dynamic
                except FileNotFoundError:
                    pass
                except Exception:
                    pass

            drv_path_str = str(drv_request.drv_path)
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
                client,
                required_paths,
                platform=drv_request.derivation.platform,
                scheduler_request_id=sched_req.id,
                derived_paths_for_request={_dp},
            )
            drv_to_build_id[drv_path_str] = build_id
            log.info(
                "build_derivation_enqueued",
                build_id=build_id,
                drv_path=drv_request.drv_path,
                scheduler_request_id=sched_req.id,
            )

        for _dp, _output_names, drv_request in resolved:
            drv_path_str = str(drv_request.drv_path)
            parsed = parsed_cache.get(drv_request.drv_path)
            if parsed is None:
                continue

            depends_on: set[int] = set()
            for input_drv in parsed.input_drvs:
                dep_id = drv_to_build_id.get(str(input_drv))
                if dep_id is not None and dep_id != drv_to_build_id.get(drv_path_str):
                    depends_on.add(dep_id)

            # Dynamic input derivations: add depends_on edges to the
            # outer build. When the trampoline fires and creates inner
            # builds, _on_build_complete will add edges to those too.
            for dyn_drv in parsed.dynamic_input_drvs:
                dep_id = drv_to_build_id.get(str(dyn_drv))
                if dep_id is not None and dep_id != drv_to_build_id.get(drv_path_str):
                    depends_on.add(dep_id)

            # Store dynamic_input_drvs on the QueuedBuild so the
            # trampoline can use it later.
            build_id = drv_to_build_id.get(drv_path_str)
            if build_id is not None and parsed.dynamic_input_drvs:
                build = self.queue.by_id.get(build_id)
                if build is not None:
                    build.dynamic_input_drvs = parsed.dynamic_input_drvs

            if depends_on:
                build_id = drv_to_build_id[drv_path_str]
                await self.queue.set_depends_on(build_id, depends_on)

        if not resolved:
            sched_req.resolve_if_done()

        result_map = await sched_req.future

        keyed_results: list[KeyedBuildResult] = []
        for dp in derived_paths:
            if isinstance(dp, DerivedPath):
                br = result_map.get(dp)
                if br is not None:
                    keyed_results.append(KeyedBuildResult(derived_path=dp, result=br))

        return BuildPathsWithResultsResponse(results=keyed_results)
