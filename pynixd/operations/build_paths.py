"""BuildPaths and BuildPathsWithResults operation types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..derived_path import DerivedPath
from ..stderr import OperationLogs
from ..store_path import StorePath
from .base import BuildMode, BuildResultStatus, KeyedBuildResult, OpRequest, OpResponse

if TYPE_CHECKING:
    from ..types import RequestContext as RequestContext

from ..types.context import ReadContext, WriteContext

# ── BuildPaths ───────────────────────────────────────────────────────


@dataclass
class BuildPathsResponse(OpResponse):
    value: int

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        obj.value = await ctx.reader.read_uint64()
        obj.logger.debug("deserialize", value=obj.value)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", value=self.value)
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(self.value)


@dataclass(kw_only=True)
class BuildPathsRequest(OpRequest[BuildPathsResponse]):
    name: ClassVar[str] = "BuildPaths"
    op: ClassVar[int] = 9
    response_type: ClassVar[type[OpResponse]] = BuildPathsResponse
    is_build: ClassVar[bool] = True
    derived_paths: set[DerivedPath]
    build_mode: BuildMode

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.derived_paths = await ctx.reader.read_string_set(DerivedPath)
        obj.build_mode = BuildMode(await ctx.reader.read_uint64())
        obj.logger.debug(
            "deserialize",
            derived_paths=obj.derived_paths,
            build_mode=obj.build_mode,
        )
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string_set(self.derived_paths)
        ctx.writer.write_uint64(self.build_mode.value)

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self.logger.debug("received_op")

        r_ctx = ReadContext(reader=ctx.proxy.r, version=ctx.version)
        self = await self.deserialize(r_ctx)

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
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        n = await ctx.reader.read_uint64()
        obj.results = []
        for _ in range(n):
            obj.results.append(await KeyedBuildResult.deserialize(ctx))
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize", results=self.results)
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(len(self.results))
        for entry in self.results:
            await entry.serialize(ctx)


@dataclass(kw_only=True)
class BuildPathsWithResultsRequest(OpRequest[BuildPathsWithResultsResponse]):
    name: ClassVar[str] = "BuildPathsWithResults"
    op: ClassVar[int] = 46
    response_type: ClassVar[type[OpResponse]] = BuildPathsWithResultsResponse
    is_build: ClassVar[bool] = True
    derived_paths: set[DerivedPath]
    build_mode: BuildMode

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.derived_paths = await ctx.reader.read_string_set(DerivedPath)
        obj.build_mode = BuildMode(await ctx.reader.read_uint64())

        obj.logger.debug(
            "deserialize",
            derived_paths=obj.derived_paths,
            build_mode=obj.build_mode,
        )
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string_set(self.derived_paths)
        ctx.writer.write_uint64(self.build_mode.value)

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self.logger.debug("received_op")

        r_ctx = ReadContext(reader=ctx.proxy.r, version=ctx.version)
        self = await self.deserialize(r_ctx)

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
