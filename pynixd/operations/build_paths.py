"""BuildPaths and BuildPathsWithResults operation types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..derived_path import DerivedPath
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import (
    BuildMode,
    KeyedBuildResult,
    OpRequest,
    OpResponse,
    RequestContext,
)

if TYPE_CHECKING:
    from ..connection import ClientConn

# ── BuildPaths ───────────────────────────────────────────────────────


@dataclass
class BuildPathsResponse(OpResponse):
    value: int = 0

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        await self.logs.from_reader(reader, client=client, buffer=buffer_logs)
        self.value = await reader.read_uint64()
        self.logger.debug("from_reader", value=self.value)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", value=self.value)
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

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.derived_paths = await reader.read_string_set(DerivedPath)
        self.build_mode = BuildMode(await reader.read_uint64())
        self.logger.debug(
            "from_reader",
            derived_paths=self.derived_paths,
            build_mode=self.build_mode,
        )
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.derived_paths)
        writer.write_uint64(self.build_mode.value)

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self.logger.debug("received_op")

        await self.from_reader(ctx.proxy.r, ctx.version)

        # Bypass scheduler if no remote stores are configured (simple proxy mode)
        if ctx.proxy.scheduler is None or not ctx.proxy.scheduler.stores:
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

        self.logger.debug("BuildPaths len(paths)=%d", len(self.derived_paths))
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
    results: list[KeyedBuildResult] = field(default_factory=list)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        await self.logs.from_reader(reader, client=client, buffer=buffer_logs)
        n = await reader.read_uint64()
        self.results = []
        for _ in range(n):
            self.results.append(await KeyedBuildResult().from_reader(reader, version))
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
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

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.derived_paths = await reader.read_string_set(DerivedPath)
        self.build_mode = BuildMode(await reader.read_uint64())

        self.logger.debug(
            "from_reader",
            derived_paths=self.derived_paths,
            build_mode=self.build_mode,
        )
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.derived_paths)
        writer.write_uint64(self.build_mode.value)

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self.logger.debug("received_op")

        await self.from_reader(ctx.proxy.r, ctx.version)

        # Bypass scheduler if no remote stores are configured (simple proxy mode)
        if ctx.proxy.scheduler is None or not ctx.proxy.scheduler.stores:
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
