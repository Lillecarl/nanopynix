"""BuildPaths and BuildPathsWithResults operation types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..derived_path import DerivedPath
from ..store_path import StorePath
from ..types import OperationLogs
from .base import (
    BuildMode,
    BuildResultStatus,
    KeyedBuildResult,
    OpRequest,
    OpResponse,
    RequestContext,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..wire import NixReader, NixWriter

# ── BuildPaths ───────────────────────────────────────────────────────


@dataclass
class BuildPathsResponse(OpResponse):
    value: int

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.from_reader(reader, client=client, buffer=buffer_logs)
        obj.value = await reader.read_uint64()
        obj.logger.debug("from_reader", value=obj.value)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", value=self.value)
        self.logs.to_writer(writer)
        writer.write_uint64(self.value)


@dataclass(kw_only=True)
class BuildPathsRequest(OpRequest[BuildPathsResponse]):
    name: ClassVar[str] = "BuildPaths"
    op: ClassVar[int] = 9
    response_type: ClassVar[type[OpResponse]] = BuildPathsResponse
    is_build: ClassVar[bool] = True
    derived_paths: set[DerivedPath]
    build_mode: BuildMode

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.derived_paths = await reader.read_string_set(DerivedPath)
        obj.build_mode = BuildMode(await reader.read_uint64())
        obj.logger.debug(
            "from_reader",
            derived_paths=obj.derived_paths,
            build_mode=obj.build_mode,
        )
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.derived_paths)
        writer.write_uint64(self.build_mode.value)

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self.logger.debug("received_op")

        self = await self.from_reader(ctx.proxy.r, ctx.version)

        if not ctx.proxy.use_scheduler_for_builds:
            self.logger.debug("handle_local_mode_fallback")
            result = await ctx.proxy.local_store.execute(self, client=ctx.proxy.client)

            # Track newly built paths
            if isinstance(result, BuildPathsWithResultsResponse):
                for kr in result.results:
                    if kr.result.status == 0:
                        for output in kr.result.built_outputs.values():
                            ctx.proxy.local_store.tracker.add_known_path(
                                StorePath(output["outPath"]).with_store_prefix(),
                            )

            self.logger.debug("responded_op")
            return result

        assert ctx.proxy.scheduler is not None
        self.logger.debug("build_paths_count", count=len(self.derived_paths))
        result = await ctx.proxy.scheduler.build_derived_paths(
            self.derived_paths,
            self.build_mode,
            client=ctx.proxy.client,
        )

        for kr in result.results:
            if kr.result.status not in (0, 1, 2):
                self.logger.debug("responded_op")
                return BuildPathsResponse(value=1)

        self.logger.debug("responded_op")
        return BuildPathsResponse(value=1)


# ── BuildPathsWithResults ────────────────────────────────────────────


@dataclass
class BuildPathsWithResultsResponse(OpResponse):
    results: list[KeyedBuildResult]

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.from_reader(reader, client=client, buffer=buffer_logs)
        n = await reader.read_uint64()
        obj.results = []
        for _ in range(n):
            obj.results.append(await KeyedBuildResult.from_reader(reader, version))
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", results=self.results)
        self.logs.to_writer(writer)
        writer.write_uint64(len(self.results))
        for entry in self.results:
            await entry.to_writer(writer, version)


@dataclass(kw_only=True)
class BuildPathsWithResultsRequest(OpRequest[BuildPathsWithResultsResponse]):
    name: ClassVar[str] = "BuildPathsWithResults"
    op: ClassVar[int] = 46
    response_type: ClassVar[type[OpResponse]] = BuildPathsWithResultsResponse
    is_build: ClassVar[bool] = True
    derived_paths: set[DerivedPath]
    build_mode: BuildMode

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.derived_paths = await reader.read_string_set(DerivedPath)
        obj.build_mode = BuildMode(await reader.read_uint64())

        obj.logger.debug(
            "from_reader",
            derived_paths=obj.derived_paths,
            build_mode=obj.build_mode,
        )
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.derived_paths)
        writer.write_uint64(self.build_mode.value)

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self.logger.debug("received_op")

        self = await self.from_reader(ctx.proxy.r, ctx.version)

        if not ctx.proxy.use_scheduler_for_builds:
            self.logger.debug("handle_local_mode_fallback")
            result = await ctx.proxy.local_store.execute(self, client=ctx.proxy.client)

            # Track newly built paths
            if isinstance(result, BuildPathsWithResultsResponse):
                for kr in result.results:
                    if kr.result.status in (
                        BuildResultStatus.BUILT,
                        BuildResultStatus.SUBSTITUTED,
                        BuildResultStatus.ALREADY_VALID,
                        BuildResultStatus.RESOLVES_TO_ALREADY_VALID,
                    ):
                        for output in kr.result.built_outputs.values():
                            ctx.proxy.local_store.tracker.add_known_path(
                                StorePath(output["outPath"]).with_store_prefix(),
                            )

            self.logger.debug("responded_op")
            return result

        assert ctx.proxy.scheduler is not None
        self.logger.debug(
            "build_paths_with_results_decomposed",
            num_derivations=len(self.derived_paths),
        )
        result = await ctx.proxy.scheduler.build_derived_paths(
            self.derived_paths,
            self.build_mode,
            client=ctx.proxy.client,
        )

        for kr in result.results:
            if kr.result.status not in (0, 1, 2):
                self.logger.warning(
                    "unexpected_build_paths_with_results_status",
                    status=kr.result.status,
                    error_msg=kr.result.error_msg,
                )

        self.logger.debug("responded_op")
        return result
