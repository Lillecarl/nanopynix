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

        # 1. Memory cache
        if proxy.local_store.has_path(request.path):
            return IsValidPathResponse(valid=True)

        # 2. SQLite fast path
        if proxy.local_store.db:
            result = await proxy.local_store.db.is_valid_path(request.path)
            if result is not None:
                return result

        # 3. Daemon fallback
        return await proxy.local_store.call(request, client=proxy._client)


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

        # 1. SQLite fast path
        if proxy.local_store.db:
            result = await proxy.local_store.db.query_path_info(request.path)
            if result is not None:
                return result

        # 2. Daemon fallback
        resp = await proxy.local_store.call(request, client=proxy._client)
        if resp.valid and resp.info is not None:
            # Protocol sends path-less info, we restore it for the proxy
            resp.info.path = request.path
        return resp


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

        # 1. SQLite fast path
        if proxy.local_store.db:
            result = await proxy.local_store.db.query_valid_paths(request.paths)
            if result is not None:
                # If substitution is requested, DB hits alone aren't sufficient
                # unless all paths were found in SQLite.
                if not request.substitute or result.paths >= request.paths:
                    return result

        # 2. Daemon fallback
        return await proxy.local_store.call(request, client=proxy._client)


# ── QueryPathFromHashPart ────────────────────────────────────────────


@dataclass
class QueryPathFromHashPartRequest(SingleStringRequest[SingleStringResponse]):
    op: ClassVar[int] = Op.QueryPathFromHashPart
    response_type: ClassVar[type[OpResponse]] = SingleStringResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> SingleStringResponse:
        request = await cls.from_reader(proxy._r, proxy._version)

        # 1. SQLite fast path
        if proxy.local_store.db:
            path = await proxy.local_store.db.query_path_from_hash_part(request.path)
            if path is not None:
                return SingleStringResponse(value=path)

        # 2. Daemon fallback
        return await proxy.local_store.call(request, client=proxy._client)


# ── QueryReferrers ────────────────────────────────────────────────────


@dataclass
class QueryReferrersRequest(SingleStringRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryReferrers
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> StringSetResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy.local_store.call(request, client=proxy._client)


# ── QueryValidDerivers ───────────────────────────────────────────────


@dataclass
class QueryValidDeriversRequest(SingleStringRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryValidDerivers
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> StringSetResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy.local_store.call(request, client=proxy._client)


# ── QueryDerivationOutputMap ─────────────────────────────────────────


@dataclass
class QueryDerivationOutputMapRequest(SingleStringRequest[StringMapResponse]):
    op: ClassVar[int] = Op.QueryDerivationOutputMap
    response_type: ClassVar[type[OpResponse]] = StringMapResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> StringMapResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy.local_store.call(request, client=proxy._client)


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

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> QuerySubstPathInfoResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy.local_store.call(request, client=proxy._client)


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
    async def handle(cls, proxy: DaemonProxy) -> NarFromPathResponse | None:
        from ..protocol import op_log

        request = await cls.from_reader(proxy._r, proxy._version)
        if await proxy.local_store.is_valid_path(request.path):
            op_log("NarFromPath").debug(
                "NarFromPath %s -> streaming to client",
                request.path,
            )
            # 1. Flush any pending output to the client
            await proxy._client.flush()
            # 2. Protocol expects STDERR_LAST before raw NAR bytes
            proxy._w.write_uint64(wire.STDERR_LAST)
            # 3. Stream from local store to client
            await proxy.local_store.stream_nar_from_path(
                path=request.path,
                dst=proxy._w,
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
        # 1. SQLite fast path
        if proxy.local_store.db:
            result = await proxy.local_store.db.query_all_valid_paths()
            if result is not None:
                proxy.local_store.add_known_paths(result.paths)
                return result

        # 2. Daemon fallback
        # No reader needed, it's an EmptyRequest
        resp = await proxy.local_store.call(cls(), client=proxy._client)
        proxy.local_store.add_known_paths(resp.paths)
        return resp


# ── QuerySubstitutablePaths ──────────────────────────────────────────


@dataclass
class QuerySubstitutablePathsRequest(StringSetRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QuerySubstitutablePaths
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> StringSetResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy.local_store.call(request, client=proxy._client)


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

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> QuerySubstPathInfosResponse:
        request = await cls.from_reader(proxy._r, proxy._version)
        return await proxy.local_store.call(request, client=proxy._client)


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
