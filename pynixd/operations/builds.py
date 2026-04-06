"""
Build operation request/response types.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..derived_path import DerivedPath

if TYPE_CHECKING:
    from ..proxy import DaemonProxy
from ..protocol import Op, op_log
from ..wire import NixReader, NixWriter
from .base import (
    BasicDerivation,
    BuildMode,
    BuildResult,
    OpRequest,
    OpResponse,
    Uint64Response,
)

log = structlog.get_logger(__name__)

# ── Shared structures ─────────────────────────────────────────────────


@dataclass
class KeyedBuildResult:
    derived_path: str = ""
    result: BuildResult = field(default_factory=BuildResult)


@dataclass
class KeyedBuildResultsResponse(OpResponse):
    """Response for BuildPathsWithResults."""

    results: list[KeyedBuildResult] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        results = []
        for _ in range(n):
            derived_path = await reader.read_string()
            result = await BuildResult.from_reader(reader, version)
            results.append(KeyedBuildResult(derived_path=derived_path, result=result))
        return cls(results=results)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.results))
        for entry in self.results:
            writer.write_string(entry.derived_path)
            await entry.result.to_writer(writer, version)


@dataclass
class BuildDerivationResponse(OpResponse):
    result: BuildResult = field(default_factory=BuildResult)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(result=await BuildResult.from_reader(reader, version))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        await self.result.to_writer(writer, version)


# ── BuildPaths / BuildPathsWithResults ───────────────────────────────


@dataclass
class BuildPathsRequest(OpRequest[Uint64Response]):
    op: ClassVar[int] = Op.BuildPaths
    response_type: ClassVar[type[OpResponse]] = Uint64Response
    is_build: ClassVar[bool] = True
    derived_paths: set[DerivedPath] = field(default_factory=set)
    build_mode: BuildMode = BuildMode.NORMAL

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            derived_paths=await reader.read_string_set(DerivedPath),
            build_mode=BuildMode(await reader.read_uint64()),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(set(self.derived_paths))
        writer.write_uint64(self.build_mode)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> OpResponse | None:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        if proxy.scheduler is None:
            log.debug("handle_local_mode_fallback")
            return await proxy.local_store.execute(request, client=proxy.client)

        from .build_planner import decompose_build_paths

        op_log("BuildPaths").debug(
            "BuildPaths len(paths)=%d", len(request.derived_paths)
        )
        decomposed = await decompose_build_paths(
            request,
            proxy.local_store,
            proxy.scheduler,
            client=proxy.client,
        )

        if not decomposed:
            return Uint64Response(value=0)  # nothing to build

        # Await all futures
        futures = [f for _, _, f in decomposed]
        responses = await asyncio.gather(*futures)

        # Any failure → overall failure
        for resp in responses:
            if isinstance(resp, BuildDerivationResponse):
                if resp.result.status != 0:
                    return Uint64Response(value=1)

        return Uint64Response(value=0)


@dataclass
class BuildPathsWithResultsRequest(OpRequest[KeyedBuildResultsResponse]):
    op: ClassVar[int] = Op.BuildPathsWithResults
    response_type: ClassVar[type[OpResponse]] = KeyedBuildResultsResponse
    is_build: ClassVar[bool] = True
    derived_paths: set[DerivedPath] = field(default_factory=set)
    build_mode: BuildMode = BuildMode.NORMAL

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            derived_paths=await reader.read_string_set(DerivedPath),
            build_mode=BuildMode(await reader.read_uint64()),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(set(self.derived_paths))
        writer.write_uint64(self.build_mode)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> OpResponse | None:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        if proxy.scheduler is None:
            log.debug("handle_local_mode_fallback")
            return await proxy.local_store.execute(request, client=proxy.client)

        from .build_planner import decompose_build_paths

        op_log("BuildPathsWithResults").debug(
            "build_paths_with_results_decomposed",
            num_derivations=len(request.derived_paths),
        )
        decomposed = await decompose_build_paths(
            request,
            proxy.local_store,
            proxy.scheduler,
            client=proxy.client,
        )

        if not decomposed:
            return KeyedBuildResultsResponse(results=[])

        # Await all futures
        futures = [f for _, _, f in decomposed]
        responses = await asyncio.gather(*futures)

        # Compose KeyedBuildResults from individual BuildDerivationResponses
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
                    from ..stderr import StderrNext

                    proxy.client.queue.put_nowait(
                        StderrNext(text=f"pynixd: {resp.result.error_msg}\n")
                    )

        return KeyedBuildResultsResponse(results=keyed_results)


# ── BuildDerivation ──────────────────────────────────────────────────


@dataclass
class BuildDerivationRequest(OpRequest[BuildDerivationResponse]):
    op: ClassVar[int] = Op.BuildDerivation
    response_type: ClassVar[type[OpResponse]] = BuildDerivationResponse
    is_build: ClassVar[bool] = True
    drv_path: str = ""
    derivation: BasicDerivation = field(default_factory=BasicDerivation)
    build_mode: BuildMode = BuildMode.NORMAL

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            drv_path=await reader.read_string(),
            derivation=await BasicDerivation.from_reader(reader, version),
            build_mode=BuildMode(await reader.read_uint64()),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.drv_path)
        await self.derivation.to_writer(writer, version)
        writer.write_uint64(self.build_mode)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> OpResponse | None:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        if proxy.scheduler is None:
            log.debug("handle_local_mode_fallback")
            return await proxy.local_store.execute(request, client=proxy.client)

        # Discover paths that exist on the local store but aren't tracked.
        unknown = (
            set(request.derivation.input_srcs) | {request.drv_path}
        ) - proxy.local_store.known_paths
        if unknown:
            valid = await proxy.local_store.query_valid_paths(unknown)
            proxy.local_store.add_known_paths(valid, update_regtime=False)

        from .build_planner import enqueue_build_derivation

        future = await enqueue_build_derivation(
            request,
            proxy.local_store,
            proxy.scheduler,
            client=proxy.client,
        )
        response = await future

        if isinstance(response, BuildDerivationResponse):
            if response.result.status != 0 and response.result.error_msg:
                from ..stderr import StderrNext

                proxy.client.queue.put_nowait(
                    StderrNext(text=f"pynixd: {response.result.error_msg}\n")
                )
        return response
