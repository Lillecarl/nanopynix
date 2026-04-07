"""
Query operation request/response types.

These operations query information from the store without mutating it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from .. import wire
from ..derived_path import DerivedPath
from ..store_path import StorePath

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..proxy import DaemonProxy
    from ..store import Store
from ..protocol import Op
from ..wire import NixReader, NixWriter
from .base import (
    ByteCollector,
    EmptyRequest,
    OpRequest,
    OpResponse,
    PathInfo,
    SingleStringRequest,
    StorePathResponse,
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

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> IsValidPathResponse:
        # 1. Memory cache
        if store.has_path(self.path):
            return IsValidPathResponse(valid=True)

        # 2. SQLite fast path
        if store.db:
            result = await store.db.is_valid_path(self.path)
            if result is not None and result.valid:
                store.add_known_path(self.path)
                return result

        # 3. Daemon fallback (Base class execute)
        resp = await super().execute(
            store,
            client,
            suppress_last,
        )
        if resp.valid:
            store.add_known_path(self.path)
        return resp


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

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryPathInfoResponse:
        # 1. SQLite fast path
        if store.db:
            result = await store.db.query_path_info(self.path)
            if result is not None and result.valid:
                store.add_known_path(self.path)
                if result.info:
                    result.info.path = self.path
                return result

        # 2. Daemon fallback
        resp = await super().execute(
            store,
            client,
            suppress_last,
        )
        if resp.valid:
            store.add_known_path(self.path)
            if resp.info is not None:
                # Protocol sends path-less info, we restore it
                resp.info.path = self.path
        return resp


# ── QueryPathInfos ───────────────────────────────────────────────────


@dataclass
class QueryPathInfosResponse(OpResponse):
    infos: dict[StorePath, PathInfo] = field(default_factory=dict)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        infos = {}
        for _ in range(n):
            info = await PathInfo.from_reader_keyed(reader)
            infos[info.path] = info
        return cls(infos=infos)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.infos))
        for info in self.infos.values():
            await info.to_writer_keyed(writer)


@dataclass
class QueryPathInfosRequest(OpRequest[QueryPathInfosResponse]):
    op: ClassVar[int] = Op.QueryPathInfos
    response_type: ClassVar[type[OpResponse]] = QueryPathInfosResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(paths=await reader.read_string_set(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryPathInfosResponse:
        # 1. SQLite fast path
        if store.db:
            result = await store.db.query_path_infos(self.paths)
            if result is not None:
                store.add_known_paths(set(result.keys()))
                return QueryPathInfosResponse(infos=result)

        # 2. Custom feature check
        async with store.transfer_conn() as conn:
            if "QueryPathInfos" in conn.features:
                return await conn.call(self, client=client, suppress_last=suppress_last)

        # 3. Sequential fallback
        infos: dict[StorePath, PathInfo] = {}
        for path in self.paths:
            resp = await store.execute(
                QueryPathInfoRequest(path=path),
                client=client,
                suppress_last=suppress_last,
            )
            if resp.valid and resp.info:
                infos[path] = resp.info
        return QueryPathInfosResponse(infos=infos)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> QueryPathInfosResponse:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        return await proxy.local_store.execute(request, client=proxy.client)


# ── QueryClosure ──────────────────────────────────────────────────────


@dataclass
class QueryClosureResponse(OpResponse):
    paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(paths=await reader.read_string_set(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)


@dataclass
class QueryClosureRequest(OpRequest[QueryClosureResponse]):
    op: ClassVar[int] = Op.QueryClosure
    response_type: ClassVar[type[OpResponse]] = QueryClosureResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(paths=await reader.read_string_set(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryClosureResponse:
        # Just use QueryClosureWithInfo and extract paths
        resp = await store.execute(
            QueryClosureWithInfoRequest(paths=self.paths),
            client=client,
            suppress_last=suppress_last,
        )
        return QueryClosureResponse(paths={info.path for info in resp.infos})

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> QueryClosureResponse:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        return await proxy.local_store.execute(request, client=proxy.client)


# ── QueryClosureWithInfo ─────────────────────────────────────────────


@dataclass
class QueryClosureWithInfoResponse(OpResponse):
    infos: list[PathInfo] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        infos = []
        for _ in range(n):
            infos.append(await PathInfo.from_reader_keyed(reader))
        return cls(infos=infos)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.infos))
        for info in self.infos:
            await info.to_writer_keyed(writer)


@dataclass
class QueryClosureWithInfoRequest(OpRequest[QueryClosureWithInfoResponse]):
    op: ClassVar[int] = Op.QueryClosureWithInfo
    response_type: ClassVar[type[OpResponse]] = QueryClosureWithInfoResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(paths=await reader.read_string_set(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryClosureWithInfoResponse:
        # 1. SQLite fast path
        if store.db:
            result = await store.db.query_closure_with_info(self.paths)
            if result is not None:
                store.add_known_paths({info.path for info in result})
                return QueryClosureWithInfoResponse(infos=result)

        # 2. Custom feature check
        async with store.transfer_conn() as conn:
            if "QueryClosureWithInfo" in conn.features:
                return await conn.call(self, client=client, suppress_last=suppress_last)

        # 3. Fallback: BFS + Topo sort
        all_infos: dict[StorePath, PathInfo] = {}
        pending = self.paths

        while pending:
            to_fetch = {p for p in pending if p not in all_infos}
            if not to_fetch:
                break

            # Use QueryPathInfos which might use DB or feature
            resp = await store.execute(
                QueryPathInfosRequest(paths=to_fetch),
                client=client,
                suppress_last=suppress_last,
            )
            new_infos = resp.infos

            for p in to_fetch:
                if p not in new_infos:
                    # If any path is missing, we can't complete the closure.
                    raise ValueError(f"Path {p} not found in store closure")

            all_infos.update(new_infos)

            # Find new references to explore
            next_pending = set()
            for info in new_infos.values():
                for ref in info.references:
                    if ref not in all_infos:
                        next_pending.add(ref)
            pending = next_pending

        # Topological sort
        sorted_infos: list[PathInfo] = []
        visited: set[StorePath] = set()
        visiting: set[StorePath] = set()

        def visit(p: StorePath):
            if p in visited:
                return
            if p in visiting:
                return  # Should not happen in Nix store
            visiting.add(p)
            info = all_infos[p]
            for ref in info.references:
                if ref != p:
                    visit(ref)
            visiting.remove(p)
            visited.add(p)
            sorted_infos.append(info)

        for p in sorted(all_infos.keys()):
            visit(p)

        return QueryClosureWithInfoResponse(infos=sorted_infos)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> QueryClosureWithInfoResponse:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        return await proxy.local_store.execute(request, client=proxy.client)


# ── QueryValidPaths ──────────────────────────────────────────────────


@dataclass
class QueryValidPathsRequest(OpRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryValidPaths
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True
    paths: set[StorePath] = field(default_factory=set)
    substitute: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        paths = await reader.read_string_set(StorePath)
        substitute = 0
        if version >= wire.proto(1, 27):
            substitute = await reader.read_uint64()
        return cls(paths=paths, substitute=substitute)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.paths)
        if version >= wire.proto(1, 27):
            writer.write_uint64(self.substitute)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> StringSetResponse:
        # 1. SQLite fast path
        if store.db:
            result = await store.db.query_valid_paths(self.paths)
            if result is not None:
                # If substitution is requested, DB hits alone aren't sufficient
                # unless all paths were found in SQLite.
                if not self.substitute or result.paths >= self.paths:
                    store.add_known_paths(result.paths)
                    return result

        # 2. Daemon fallback
        resp = await super().execute(
            store,
            client,
            suppress_last,
        )
        store.add_known_paths(resp.paths)
        return resp


# ── QueryPathFromHashPart ────────────────────────────────────────────


@dataclass
class QueryPathFromHashPartRequest(SingleStringRequest[StorePathResponse]):
    op: ClassVar[int] = Op.QueryPathFromHashPart
    response_type: ClassVar[type[OpResponse]] = StorePathResponse
    is_query: ClassVar[bool] = True

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> StorePathResponse:
        # 1. SQLite fast path
        if store.db:
            path = await store.db.query_path_from_hash_part(self.path)
            if path is not None:
                store.add_known_path(path)
                return StorePathResponse(value=path)

        # 2. Daemon fallback
        resp = await super().execute(
            store,
            client,
            suppress_last,
        )
        if resp.value:
            store.add_known_path(StorePath(resp.value))
        return resp


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
    async def handle(cls, proxy: DaemonProxy) -> NarFromPathResponse | None:
        from ..protocol import op_log

        structlog.contextvars.bind_contextvars(operation=cls.__name__)

        request = await cls.from_reader(proxy.r, proxy.version)
        is_valid_resp = await proxy.local_store.execute(
            IsValidPathRequest(path=request.path)
        )
        if is_valid_resp.valid:
            op_log("NarFromPath").debug(
                "nar_from_path_streaming",
                path=request.path,
            )
            # 1. Flush any pending output to the client
            await proxy.client.flush()
            # 2. Protocol expects STDERR_LAST before raw NAR bytes
            proxy.w.write_uint64(wire.STDERR_LAST)
            # 3. Stream from local store to client
            await proxy.local_store.stream_nar_from_path(
                path=request.path,
                dst=proxy.w,
            )
            await proxy.w.drain()
            return None

        cls._log.warning("nar_not_in_local_store", path=request.path)
        return NarFromPathResponse(nar_data=b"")


# ── QueryAllValidPaths ───────────────────────────────────────────────


@dataclass
class QueryAllValidPathsRequest(EmptyRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryAllValidPaths
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> StringSetResponse:
        # 1. SQLite fast path
        if store.db:
            result = await store.db.query_all_valid_paths()
            if result is not None:
                store.add_known_paths(result.paths, update_regtime=False)
                return result

        # 2. Daemon fallback
        resp = await super().execute(
            store,
            client,
            suppress_last,
        )
        store.add_known_paths(resp.paths, update_regtime=False)
        return resp


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
    will_build: set[StorePath] = field(default_factory=set)
    will_substitute: set[StorePath] = field(default_factory=set)
    unknown: set[StorePath] = field(default_factory=set)
    download_size: int = 0
    nar_size: int = 0

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            will_build=await reader.read_string_set(StorePath),
            will_substitute=await reader.read_string_set(StorePath),
            unknown=await reader.read_string_set(StorePath),
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
        writer.write_string_set(self.derived_paths)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryMissingResponse:
        """Query which paths are missing from this store."""
        resp = await super().execute(
            store,
            client,
            suppress_last,
        )

        # Update known paths: outputs of all derived paths are now expected
        if store.store_path:
            for dp in self.derived_paths:
                outputs = dp.to_outputs(store.store_path)
                store.add_known_paths(outputs)

        # Any path being substituted is also "known" to be available.
        # TODO: This could be optimized by running in a background task,
        # but for now we await it inline to ensure paths are registered
        # before the scheduler runs.
        if resp.will_substitute:
            try:
                async with store.transfer_conn() as conn:
                    valid = await conn.call(
                        QueryValidPathsRequest(
                            paths=resp.will_substitute,
                            substitute=1,
                        )
                    )
                    store.add_known_paths(valid.paths)
            except Exception:
                self._log.debug(
                    "verify_substitutable_failed",
                    paths=len(resp.will_substitute),
                    exc_info=True,
                )

        return resp
