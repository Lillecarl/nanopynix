"""BuildPaths and BuildPathsWithResults operation types."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..derived_path import DerivedPath
from ..drv_parser import read_drv_file, to_basic_derivation
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import (
    BuildMode,
    KeyedBuildResult,
    OpRequest,
    OpResponse,
    OperationLogs,
)
from .build_derivation import BuildDerivationRequest, BuildDerivationResponse
from .query_derivation_outputs_batch import QueryDerivationOutputsBatchRequest
from .query_missing import QueryMissingRequest
from .query_valid_paths import QueryValidPathsRequest
from ..stderr import StderrNext

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..drv_parser import ParsedDerivation
    from ..proxy import DaemonProxy
    from ..scheduler import Scheduler
    from ..store import Store

log = structlog.get_logger(__name__)


# ── Decomposition (private) ──────────────────────────────────────────


async def _decompose_build_paths(
    request: BuildPathsRequest | BuildPathsWithResultsRequest,
    store: Store,
    scheduler: Scheduler,
    client: ClientConn,
) -> list[tuple[DerivedPath, set[str], asyncio.Future]]:
    """Decompose high-level build requests into individual BuildDerivation futures."""
    missing_resp = await store.execute(
        QueryMissingRequest(derived_paths=request.derived_paths)
    )

    results: list[tuple[DerivedPath, set[str], asyncio.Future]] = []

    parsed_cache: dict[StorePath, ParsedDerivation] = {}
    all_planned_outputs: set[StorePath] = set()
    all_input_drvs: set[StorePath] = set()

    for dp in (DerivedPath(p) for p in missing_resp.will_build):
        try:
            parsed = dp.to_derivation(store.store_path)
        except FileNotFoundError:
            log.warning("drv_read_failed", drv_path=dp.drv_path)
            continue

        parsed_cache[StorePath(dp.drv_path)] = parsed
        all_planned_outputs.update(parsed.output_paths().values())
        all_input_drvs.update(parsed.input_drvs.keys())

    output_cache = None
    if all_input_drvs:
        resp = await store.execute(
            QueryDerivationOutputsBatchRequest(drv_paths=all_input_drvs)
        )
        output_cache = resp.outputs if resp.outputs else {}

    resolved: list[tuple[DerivedPath, set[str], BuildDerivationRequest]] = []
    all_input_srcs: set[StorePath] = set()

    for dp in (DerivedPath(p) for p in missing_resp.will_build):
        drv_path = StorePath(dp.drv_path)
        parsed = parsed_cache.get(drv_path)
        if parsed is None:
            continue

        basic = to_basic_derivation(parsed, store.store_path, output_cache=output_cache)
        drv_request = BuildDerivationRequest(
            drv_path=drv_path,
            derivation=basic,
            build_mode=request.build_mode,
        )
        resolved.append((dp, dp.output_names, drv_request))
        all_input_srcs.update(basic.input_srcs)

    unknown = all_input_srcs - store.known_paths
    if unknown:
        valid_resp = await store.execute(QueryValidPathsRequest(paths=unknown))
        store.add_known_paths(valid_resp.paths, update_regtime=False)

    for dp, output_names, drv_request in resolved:
        # Enrich with .drv metadata
        if drv_request.drv_path in parsed_cache:
            drv_request.derivation.is_dynamic = parsed_cache[
                drv_request.drv_path
            ].is_dynamic
        else:
            try:
                parsed = read_drv_file(store.store_path, drv_request.drv_path)
                drv_request.derivation.is_dynamic = parsed.is_dynamic
            except FileNotFoundError:
                pass
            except Exception:
                pass

        drv_path_str = str(drv_request.drv_path)
        required_paths: set[StorePath] = set()
        for inp in drv_request.derivation.input_srcs:
            required_paths.add(StorePath(inp, extrainfo=f"input_src of {drv_path_str}"))
        required_paths.add(
            StorePath(drv_request.drv_path, extrainfo=f"drv_path of {drv_path_str}")
        )
        build_id, future = await scheduler.enqueue(
            drv_request,
            client,
            required_paths,
            platform=drv_request.derivation.platform,
        )
        log.info(
            "build_derivation_enqueued",
            build_id=build_id,
            drv_path=drv_request.drv_path,
        )

        results.append((dp, output_names, future))

    return results


# ── BuildPaths ───────────────────────────────────────────────────────


@dataclass
class BuildPathsResponse(OpResponse):
    value: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            logs=await OperationLogs.from_reader(reader),
            value=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger.debug("to_writer", self.value)
        self.logs.to_writer(writer)
        writer.write_uint64(self.value)


@dataclass
class BuildPathsRequest(OpRequest[BuildPathsResponse]):
    name: ClassVar[str] = "BuildPaths"
    op: ClassVar[int] = 9
    response_type: ClassVar[type[OpResponse]] = BuildPathsResponse
    is_build: ClassVar[bool] = True
    derived_paths: set[DerivedPath] = field(default_factory=set)
    build_mode: BuildMode = BuildMode.NORMAL

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        derived_paths = await reader.read_string_set(DerivedPath)
        build_mode = BuildMode(await reader.read_uint64())
        cls.logger.debug(
            "from_reader", derived_paths=derived_paths, build_mode=build_mode
        )
        return cls(
            derived_paths=derived_paths,
            build_mode=build_mode,
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string_set(self.derived_paths)
        writer.write_uint64(self.build_mode)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> OpResponse | None:
        log = structlog.get_logger(f"pynixd.operations.{cls.__name__}")
        log.debug("received_op")
        request = await cls.from_reader(proxy.r, proxy.version)
        if proxy.scheduler is None:
            log.debug("handle_local_mode_fallback")
            result = await proxy.local_store.execute(request, client=proxy.client)
            log.debug("responded_op")
            return result

        log.debug("BuildPaths len(paths)=%d", len(request.derived_paths))
        decomposed = await _decompose_build_paths(
            request,
            proxy.local_store,
            proxy.scheduler,
            client=proxy.client,
        )

        if not decomposed:
            log.debug("responded_op")
            return BuildPathsResponse(value=0)

        futures = [f for _, _, f in decomposed]
        responses = await asyncio.gather(*futures)

        for resp in responses:
            if isinstance(resp, BuildDerivationResponse):
                if resp.result.status != 0:
                    log.debug("responded_op")
                    return BuildPathsResponse(value=1)

        log.debug("responded_op")
        return BuildPathsResponse(value=0)


# ── BuildPathsWithResults ────────────────────────────────────────────


@dataclass
class BuildPathsWithResultsResponse(OpResponse):
    results: list[KeyedBuildResult] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        logs = await OperationLogs.from_reader(reader)
        n = await reader.read_uint64()
        results = []
        for _ in range(n):
            results.append(await KeyedBuildResult.from_reader(reader, version))
        return cls(logs=logs, results=results)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger.debug("to_writer", self.results)
        self.logs.to_writer(writer)
        writer.write_uint64(len(self.results))
        for entry in self.results:
            await entry.to_writer(writer, version)


@dataclass
class BuildPathsWithResultsRequest(OpRequest[BuildPathsWithResultsResponse]):
    name: ClassVar[str] = "BuildPathsWithResults"
    op: ClassVar[int] = 46
    response_type: ClassVar[type[OpResponse]] = BuildPathsWithResultsResponse
    is_build: ClassVar[bool] = True
    derived_paths: set[DerivedPath] = field(default_factory=set)
    build_mode: BuildMode = BuildMode.NORMAL

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        derived_paths = await reader.read_string_set(DerivedPath)
        build_mode = BuildMode(await reader.read_uint64())

        cls.logger.debug(
            "from_reader", derived_paths=derived_paths, build_mode=build_mode
        )
        return cls(
            derived_paths=derived_paths,
            build_mode=build_mode,
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string_set(self.derived_paths)
        writer.write_uint64(self.build_mode)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> OpResponse | None:
        log = structlog.get_logger(f"pynixd.operations.{cls.__name__}")
        log.debug("received_op")
        request = await cls.from_reader(proxy.r, proxy.version)
        if proxy.scheduler is None:
            log.debug("handle_local_mode_fallback")
            result = await proxy.local_store.execute(request, client=proxy.client)
            log.debug("responded_op")
            return result

        log.debug(
            "build_paths_with_results_decomposed",
            num_derivations=len(request.derived_paths),
        )
        decomposed = await _decompose_build_paths(
            request,
            proxy.local_store,
            proxy.scheduler,
            client=proxy.client,
        )

        if not decomposed:
            log.debug("responded_op")
            return BuildPathsWithResultsResponse(results=[])

        futures = [f for _, _, f in decomposed]
        responses = await asyncio.gather(*futures)

        keyed_results: list[KeyedBuildResult] = []
        for (dp, _, _), resp in zip(decomposed, responses):
            if isinstance(resp, BuildDerivationResponse):
                keyed_results.append(
                    KeyedBuildResult(
                        derived_path=dp,
                        result=resp.result,
                    )
                )
                if resp.result.status not in (0, 1, 2):
                    log.warning(
                        "unexpected_build_paths_with_results_status",
                        status=resp.result.status,
                        error_msg=resp.result.error_msg,
                    )
                if resp.result.status != 0 and resp.result.error_msg and proxy.client:
                    proxy.client.queue.put_nowait(
                        StderrNext(text=f"pynixd: {resp.result.error_msg}\n")
                    )

        log.debug("responded_op")
        return BuildPathsWithResultsResponse(results=keyed_results)
