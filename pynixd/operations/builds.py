"""
Build operation request/response types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..derived_path import DerivedPath

if TYPE_CHECKING:
    from ..proxy import DaemonProxy
from ..protocol import Op
from ..wire import NixReader, NixWriter
from .base import (
    BasicDerivation,
    BuildMode,
    BuildResult,
    OpRequest,
    OpResponse,
    Uint64Response,
)

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
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy._build_paths(request)


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
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy._build_paths_with_results(request)


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
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy._build_derivation(request)
