"""
Query operation request/response types.

These operations query information from the store without mutating it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..derived_path import DerivedPath

if TYPE_CHECKING:
    from ..proxy import DaemonProxy
from ..protocol import Op
from ..wire import NixReader, NixWriter
from .base import (
    ByteCollector,
    EmptyRequest,
    OpRequest,
    OpResponse,
    PathInfo,
    SingleStringRequest,
    SingleStringResponse,
    StringMapRequest,
    StringMapResponse,
    StringSetRequest,
    StringSetResponse,
    SubstPathInfo,
)

# ── IsValidPath ──────────────────────────────────────────────────────


@dataclass
class IsValidPathResponse(OpResponse):
    valid: bool = False

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(valid=await reader.read_uint64() != 0)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(1 if self.valid else 0)


@dataclass
class IsValidPathRequest(SingleStringRequest[IsValidPathResponse]):
    op: ClassVar[int] = Op.IsValidPath
    response_type: ClassVar[type[OpResponse]] = IsValidPathResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> IsValidPathResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        valid = await proxy.local_store.is_valid_path(request.path)
        return IsValidPathResponse(valid=valid)


# ── QueryPathInfo ────────────────────────────────────────────────────


@dataclass
class QueryPathInfoResponse(OpResponse):
    valid: bool = False
    info: PathInfo | None = None

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        valid = await reader.read_uint64() != 0
        info = None
        if valid:
            info = await PathInfo.from_reader_unkeyed(reader)
        return cls(valid=valid, info=info)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(1 if self.valid else 0)
        if self.valid and self.info is not None:
            await self.info.to_writer_unkeyed(writer)


@dataclass
class QueryPathInfoRequest(SingleStringRequest[QueryPathInfoResponse]):
    op: ClassVar[int] = Op.QueryPathInfo
    response_type: ClassVar[type[OpResponse]] = QueryPathInfoResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> QueryPathInfoResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        info = await proxy.local_store.query_path_info(request.path)
        return QueryPathInfoResponse(valid=info is not None, info=info)


# ── QueryValidPaths ──────────────────────────────────────────────────


@dataclass
class QueryValidPathsRequest(OpRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryValidPaths
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True
    paths: set[str] = field(default_factory=set)
    substitute: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        paths = await reader.read_string_set()
        substitute = 0
        if version >= wire.proto(1, 27):
            substitute = await reader.read_uint64()
        return cls(paths=paths, substitute=substitute)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)
        if version >= wire.proto(1, 27):
            writer.write_uint64(self.substitute)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> StringSetResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        paths = await proxy.local_store.query_valid_paths(
            request.paths, substitute=bool(request.substitute)
        )
        return StringSetResponse(paths=paths)


# ── QueryPathFromHashPart ────────────────────────────────────────────


@dataclass
class QueryPathFromHashPartRequest(SingleStringRequest[SingleStringResponse]):
    op: ClassVar[int] = Op.QueryPathFromHashPart
    response_type: ClassVar[type[OpResponse]] = SingleStringResponse
    is_query: ClassVar[bool] = True


# ── QueryReferrers ────────────────────────────────────────────────────


@dataclass
class QueryReferrersRequest(SingleStringRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryReferrers
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True


# ── QueryValidDerivers ───────────────────────────────────────────────


@dataclass
class QueryValidDeriversRequest(SingleStringRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryValidDerivers
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True


# ── QueryDerivationOutputMap ─────────────────────────────────────────


@dataclass
class QueryDerivationOutputMapRequest(SingleStringRequest[StringMapResponse]):
    op: ClassVar[int] = Op.QueryDerivationOutputMap
    response_type: ClassVar[type[OpResponse]] = StringMapResponse
    is_query: ClassVar[bool] = True


# ── QuerySubstitutablePathInfo ───────────────────────────────────────


@dataclass
class QuerySubstPathInfoResponse(OpResponse):
    found: bool = False
    info: SubstPathInfo | None = None

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        found = await reader.read_uint64() != 0
        info = None
        if found:
            info = await SubstPathInfo.from_reader(reader, version)
        return cls(found=found, info=info)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(1 if self.found else 0)
        if self.found and self.info is not None:
            await self.info.to_writer(writer, version)


@dataclass
class QuerySubstPathInfoRequest(SingleStringRequest[QuerySubstPathInfoResponse]):
    op: ClassVar[int] = Op.QuerySubstitutablePathInfo
    response_type: ClassVar[type[OpResponse]] = QuerySubstPathInfoResponse
    is_query: ClassVar[bool] = True


# ── NarFromPath ──────────────────────────────────────────────────────


@dataclass
class NarFromPathResponse(OpResponse):
    """Response containing raw NAR data."""

    nar_data: bytes = b""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        collector = ByteCollector()
        await wire.stream_parse_nar(reader, collector, capture=False)
        return cls(nar_data=collector.getvalue())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write(self.nar_data)


@dataclass
class NarFromPathRequest(SingleStringRequest[NarFromPathResponse]):
    op: ClassVar[int] = Op.NarFromPath
    response_type: ClassVar[type[OpResponse]] = NarFromPathResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> OpResponse | None:
        from ..exceptions import BackendError
        from ..protocol import op_log

        request = await cls.from_reader(proxy._r, proxy._version)
        if await proxy.local_store.is_valid_path(request.path):
            op_log("NarFromPath").debug(
                "NarFromPath %s -> streaming to client",
                request.path,
            )
            path_info = await proxy.local_store.query_path_info(request.path)
            if path_info is None:
                raise BackendError("getting status of %s", request.path)
            await proxy._client.flush()
            proxy._w.write_uint64(wire.STDERR_LAST)
            await proxy.local_store.stream_nar_from_path(
                path=request.path,
                dst=proxy._w,
                nar_size=path_info.nar_size,
            )
            await proxy._w.drain()
            return None
        cls._log.warning("NarFromPath %s not in local store", request.path)
        return NarFromPathResponse(nar_data=b"")


# ── QueryAllValidPaths ───────────────────────────────────────────────


@dataclass
class QueryAllValidPathsRequest(EmptyRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryAllValidPaths
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> StringSetResponse:
        paths = await proxy.local_store.query_all_valid_paths()
        return StringSetResponse(paths=paths)


# ── QuerySubstitutablePaths ──────────────────────────────────────────


@dataclass
class QuerySubstitutablePathsRequest(StringSetRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QuerySubstitutablePaths
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True


# ── QuerySubstitutablePathInfos ──────────────────────────────────────


@dataclass
class SubstPathInfoEntry:
    path: str = ""
    info: SubstPathInfo = field(default_factory=SubstPathInfo)


@dataclass
class QuerySubstPathInfosResponse(OpResponse):
    entries: list[SubstPathInfoEntry] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        entries = []
        for _ in range(n):
            path = await reader.read_string()
            info = await SubstPathInfo.from_reader(reader, version)
            entries.append(SubstPathInfoEntry(path=path, info=info))
        return cls(entries=entries)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.entries))
        for entry in self.entries:
            writer.write_string(entry.path)
            await entry.info.to_writer(writer, version)


@dataclass
class QuerySubstPathInfosRequest(StringMapRequest[QuerySubstPathInfosResponse]):
    op: ClassVar[int] = Op.QuerySubstitutablePathInfos
    response_type: ClassVar[type[OpResponse]] = QuerySubstPathInfosResponse
    is_query: ClassVar[bool] = True


# ── QueryMissing ─────────────────────────────────────────────────────


@dataclass
class QueryMissingResponse(OpResponse):
    will_build: set[str] = field(default_factory=set)
    will_substitute: set[str] = field(default_factory=set)
    unknown: set[str] = field(default_factory=set)
    download_size: int = 0
    nar_size: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            will_build=await reader.read_string_set(),
            will_substitute=await reader.read_string_set(),
            unknown=await reader.read_string_set(),
            download_size=await reader.read_uint64(),
            nar_size=await reader.read_uint64(),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.will_build)
        writer.write_string_set(self.will_substitute)
        writer.write_string_set(self.unknown)
        writer.write_uint64(self.download_size)
        writer.write_uint64(self.nar_size)


@dataclass
class QueryMissingRequest(OpRequest[QueryMissingResponse]):
    op: ClassVar[int] = Op.QueryMissing
    response_type: ClassVar[type[OpResponse]] = QueryMissingResponse
    is_query: ClassVar[bool] = True
    derived_paths: set[DerivedPath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(derived_paths=await reader.read_string_set(DerivedPath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(set(self.derived_paths))

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> QueryMissingResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy.local_store.query_missing(request)
