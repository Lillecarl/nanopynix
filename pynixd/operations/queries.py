"""
Query operation request/response types.

These operations query information from the store without mutating it.
Each operation that has a DB fast-path implements ``execute_db(db)`` for
SQLite dispatch and ``execute(store)`` for the full pipeline.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from .. import wire
from ..derived_path import DerivedPath
from ..store_path import StorePath

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..local_store_db import LocalStoreDB
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

# ── SQL constants ─────────────────────────────────────────────────────
# Operations import these to run their own queries against LocalStoreDB.

IS_VALID_PATH = "SELECT 1 FROM ValidPaths WHERE path = ? LIMIT 1"

QUERY_PATH_INFO = """
SELECT path, deriver, hash, registrationTime, narSize, ultimate, sigs, ca
FROM ValidPaths WHERE path = ?
"""

QUERY_REFERENCES = """
SELECT vp.path FROM Refs r
JOIN ValidPaths vp ON r.reference = vp.id
WHERE r.referrer = (SELECT id FROM ValidPaths WHERE path = ?)
"""

QUERY_PATH_FROM_HASH_PART = """
SELECT path FROM ValidPaths WHERE path >= ? AND path < ? LIMIT 1
"""

QUERY_ALL_VALID_PATHS = "SELECT path FROM ValidPaths"

QUERY_VALID_PATHS_BATCH = (
    "SELECT path FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))"
)

QUERY_CLOSURE_WITH_INFO = """
WITH RECURSIVE closure(id) AS (
    SELECT id FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))
    UNION
    SELECT r.reference
    FROM closure c
    JOIN Refs r ON c.id = r.referrer
)
SELECT vp.path, vp.deriver, vp.hash, vp.registrationTime, vp.narSize,
       vp.ultimate, vp.sigs, vp.ca,
       (SELECT group_concat(ref_vp.path, ' ')
        FROM Refs r
        JOIN ValidPaths ref_vp ON r.reference = ref_vp.id
        WHERE r.referrer = vp.id)
FROM closure c
JOIN ValidPaths vp ON c.id = vp.id
ORDER BY vp.id ASC
"""

QUERY_PATH_INFOS_BATCH = """
SELECT vp.path, vp.deriver, vp.hash, registrationTime, narSize,
       vp.ultimate, vp.sigs, vp.ca
FROM ValidPaths vp
WHERE vp.path IN (SELECT value FROM json_each(?))
"""

QUERY_REFERENCES_BATCH = """
SELECT vp_referrer.path, vp_ref.path
FROM Refs r
JOIN ValidPaths vp_referrer ON r.referrer = vp_referrer.id
JOIN ValidPaths vp_ref ON r.reference = vp_ref.id
WHERE vp_referrer.path IN (SELECT value FROM json_each(?))
"""

QUERY_DERIVATION_OUTPUTS_BATCH = """
SELECT vp_drv.path, do.id, do.path
FROM DerivationOutputs do
JOIN ValidPaths vp_drv ON do.drv = vp_drv.id
WHERE vp_drv.path IN (SELECT value FROM json_each(?))
"""

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

    async def execute_db(self, db: LocalStoreDB) -> IsValidPathResponse | None:
        async with db.acquire_conn() as conn:
            async with conn.execute(IS_VALID_PATH, (self.path,)) as cursor:
                row = await cursor.fetchone()
        return IsValidPathResponse(valid=row is not None)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> IsValidPathResponse:
        if store.has_path(self.path):
            return IsValidPathResponse(valid=True)

        if store.db:
            result = await store.db.execute(self)
            if result is not None and result.valid:
                store.add_known_path(self.path)
                return result

        resp = await super().execute(store, client, suppress_last)
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

    async def execute_db(self, db: LocalStoreDB) -> QueryPathInfoResponse | None:
        async with db.acquire_conn() as conn:
            async with conn.execute(QUERY_PATH_INFO, (self.path,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return QueryPathInfoResponse(valid=False)

            _path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca = row

            async with conn.execute(QUERY_REFERENCES, (self.path,)) as cursor:
                ref_rows = await cursor.fetchall()
            refs = {r[0] for r in ref_rows}

        return QueryPathInfoResponse(
            valid=True,
            info=PathInfo(
                path=self.path,
                deriver=StorePath(deriver or ""),
                nar_hash=nar_hash,
                references={StorePath(r) for r in refs},
                registration_time=reg_time,
                nar_size=nar_size or 0,
                ultimate=1 if ultimate else 0,
                sigs=set(sigs.split()) if sigs else set(),
                ca=ca or "",
            ),
        )

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryPathInfoResponse:
        if store.db:
            result = await store.db.execute(self)
            if result is not None and result.valid:
                store.add_known_path(self.path)
                return result

        resp = await super().execute(store, client, suppress_last)
        if resp.valid:
            store.add_known_path(self.path)
            if resp.info is not None:
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

    async def execute_db(self, db: LocalStoreDB) -> QueryPathInfosResponse | None:
        if not self.paths:
            return QueryPathInfosResponse(infos={})

        paths_json = json.dumps(list(self.paths))
        async with db.acquire_conn() as conn:
            async with conn.execute(QUERY_PATH_INFOS_BATCH, (paths_json,)) as cursor:
                rows = await cursor.fetchall()
            async with conn.execute(QUERY_REFERENCES_BATCH, (paths_json,)) as cursor:
                ref_rows = await cursor.fetchall()

        refs_map: dict[StorePath, set[StorePath]] = {}
        for referrer, reference in ref_rows:
            refs_map.setdefault(StorePath(referrer), set()).add(StorePath(reference))

        infos: dict[StorePath, PathInfo] = {}
        for path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca in rows:
            p = StorePath(path)
            infos[p] = PathInfo(
                path=p,
                deriver=StorePath(deriver or ""),
                nar_hash=nar_hash,
                references=refs_map.get(p, set()),
                registration_time=reg_time,
                nar_size=nar_size or 0,
                ultimate=1 if ultimate else 0,
                sigs=set(sigs.split()) if sigs else set(),
                ca=ca or "",
            )
        return QueryPathInfosResponse(infos=infos)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryPathInfosResponse:
        if store.db:
            result = await store.db.execute(self)
            if result is not None:
                store.add_known_paths(set(result.infos.keys()))
                return result

        async with store.transfer_conn() as conn:
            if "QueryPathInfos" in conn.features:
                return await conn.call(self, client=client, suppress_last=suppress_last)

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

    async def execute_db(self, db: LocalStoreDB) -> QueryClosureWithInfoResponse | None:
        if not self.paths:
            return QueryClosureWithInfoResponse(infos=[])

        seeds_json = json.dumps(list(self.paths))
        async with db.acquire_conn() as conn:
            async with conn.execute(QUERY_CLOSURE_WITH_INFO, (seeds_json,)) as cursor:
                rows = await cursor.fetchall()

        sorted_infos: list[PathInfo] = []
        for (
            path,
            deriver,
            nar_hash,
            reg_time,
            nar_size,
            ultimate,
            sigs,
            ca,
            refs_str,
        ) in rows:
            p = StorePath(path)
            references = {StorePath(r) for r in refs_str.split()} if refs_str else set()
            sorted_infos.append(
                PathInfo(
                    path=p,
                    deriver=StorePath(deriver or ""),
                    nar_hash=nar_hash,
                    references=references,
                    registration_time=reg_time,
                    nar_size=nar_size or 0,
                    ultimate=1 if ultimate else 0,
                    sigs=set(sigs.split()) if sigs else set(),
                    ca=ca or "",
                )
            )

        return QueryClosureWithInfoResponse(infos=sorted_infos)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryClosureWithInfoResponse:
        if store.db:
            result = await store.db.execute(self)
            if result is not None:
                store.add_known_paths({info.path for info in result.infos})
                return result

        async with store.transfer_conn() as conn:
            if "QueryClosureWithInfo" in conn.features:
                return await conn.call(self, client=client, suppress_last=suppress_last)

        all_infos: dict[StorePath, PathInfo] = {}
        pending = self.paths

        while pending:
            to_fetch = {p for p in pending if p not in all_infos}
            if not to_fetch:
                break

            resp = await store.execute(
                QueryPathInfosRequest(paths=to_fetch),
                client=client,
                suppress_last=suppress_last,
            )
            new_infos = resp.infos

            for p in to_fetch:
                if p not in new_infos:
                    raise ValueError(f"Path {p} not found in store closure")

            all_infos.update(new_infos)

            next_pending = set()
            for info in new_infos.values():
                for ref in info.references:
                    if ref not in all_infos:
                        next_pending.add(ref)
            pending = next_pending

        sorted_infos: list[PathInfo] = []
        visited: set[StorePath] = set()
        visiting: set[StorePath] = set()

        def visit(p: StorePath):
            if p in visited:
                return
            if p in visiting:
                return
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

    async def execute_db(self, db: LocalStoreDB) -> StringSetResponse | None:
        paths_json = json.dumps(list(self.paths))
        async with db.acquire_conn() as conn:
            async with conn.execute(QUERY_VALID_PATHS_BATCH, (paths_json,)) as cursor:
                rows = await cursor.fetchall()
        return StringSetResponse(paths={StorePath(row[0]) for row in rows})

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> StringSetResponse:
        if store.db:
            result = await store.db.execute(self)
            if result is not None:
                if not self.substitute or result.paths >= self.paths:
                    store.add_known_paths(result.paths)
                    return result

        resp = await super().execute(store, client, suppress_last)
        store.add_known_paths(resp.paths)
        return resp


# ── QueryPathFromHashPart ────────────────────────────────────────────


@dataclass
class QueryPathFromHashPartRequest(SingleStringRequest[StorePathResponse]):
    op: ClassVar[int] = Op.QueryPathFromHashPart
    response_type: ClassVar[type[OpResponse]] = StorePathResponse
    is_query: ClassVar[bool] = True

    async def execute_db(self, db: LocalStoreDB) -> StorePathResponse | None:
        prefix = f"/nix/store/{self.path}"
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        async with db.acquire_conn() as conn:
            async with conn.execute(
                QUERY_PATH_FROM_HASH_PART, (prefix, upper)
            ) as cursor:
                row = await cursor.fetchone()
        return StorePathResponse(value=StorePath(row[0])) if row else None

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> StorePathResponse:
        if store.db:
            result = await store.db.execute(self)
            if result is not None and result.value:
                store.add_known_path(StorePath(result.value))
                return result

        resp = await super().execute(store, client, suppress_last)
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


# ── QueryDerivationOutputsBatch ──────────────────────────────────────
# pynixd extension: batch-lookup output paths for multiple .drv files.


@dataclass
class DerivationOutputsBatchResponse(OpResponse):
    """{drv_path: {output_name: output_path}}."""

    outputs: dict[StorePath, dict[str, StorePath]] = field(default_factory=dict)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        n = await reader.read_uint64()
        outputs: dict[StorePath, dict[str, StorePath]] = {}
        for _ in range(n):
            drv_path = await reader.read_string(StorePath)
            m = await reader.read_uint64()
            drv_outputs: dict[str, StorePath] = {}
            for _ in range(m):
                name = await reader.read_string()
                path = await reader.read_string(StorePath)
                drv_outputs[name] = path
            outputs[drv_path] = drv_outputs
        return cls(outputs=outputs)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(len(self.outputs))
        for drv_path, drv_outputs in self.outputs.items():
            writer.write_string(drv_path)
            writer.write_uint64(len(drv_outputs))
            for name, path in drv_outputs.items():
                writer.write_string(name)
                writer.write_string(path)


@dataclass
class QueryDerivationOutputsBatchRequest(OpRequest[DerivationOutputsBatchResponse]):
    op: ClassVar[int] = Op.QueryDerivationOutputsBatch
    response_type: ClassVar[type[OpResponse]] = DerivationOutputsBatchResponse
    is_query: ClassVar[bool] = True
    drv_paths: set[StorePath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(drv_paths=await reader.read_string_set(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.drv_paths)

    async def execute_db(
        self, db: LocalStoreDB
    ) -> DerivationOutputsBatchResponse | None:
        if not self.drv_paths:
            return DerivationOutputsBatchResponse(outputs={})

        paths_json = json.dumps(list(self.drv_paths))
        async with db.acquire_conn() as conn:
            async with conn.execute(
                QUERY_DERIVATION_OUTPUTS_BATCH, (paths_json,)
            ) as cursor:
                rows = await cursor.fetchall()

        result: dict[StorePath, dict[str, StorePath]] = {}
        for drv_path, output_name, output_path in rows:
            result.setdefault(StorePath(drv_path), {})[output_name] = StorePath(
                output_path
            )
        return DerivationOutputsBatchResponse(outputs=result)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> DerivationOutputsBatchResponse:
        if store.db:
            result = await store.db.execute(self)
            if result is not None:
                return result

        # Fallback: read each .drv file from disk
        outputs: dict[StorePath, dict[str, StorePath]] = {}
        for drv_path in self.drv_paths:
            try:
                from ..drv_parser import read_drv_file

                parsed = read_drv_file(store.store_path, drv_path)
                outputs[drv_path] = parsed.output_paths()
            except FileNotFoundError:
                pass
        return DerivationOutputsBatchResponse(outputs=outputs)


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
    nar_size: int = 0
    async_callback: Callable[[bytes], Awaitable[None]] | None = None

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> NarFromPathResponse:
        if self.nar_size > 0:
            from ..wire import _CHUNK_SIZE

            async with store.transfer_conn() as conn:
                conn.w.write_uint64(self.op)
                await self.to_writer(conn.w, conn.version)
                await conn.w.drain()
                await conn.r.drain_stderr()

                if self.async_callback:
                    remaining = self.nar_size
                    while remaining > 0:
                        to_read = min(remaining, _CHUNK_SIZE)
                        chunk = await conn.r.readexactly(to_read)
                        await self.async_callback(chunk)
                        remaining -= to_read
                    return NarFromPathResponse()

                data = await conn.r.readexactly(self.nar_size)
                return NarFromPathResponse(nar_data=data)

        return await super().execute(store, client, suppress_last)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> NarFromPathResponse | None:
        from ..protocol import op_log

        structlog.contextvars.bind_contextvars(operation=cls.__name__)

        request = await cls.from_reader(proxy.r, proxy.version)
        path = request.path

        info_resp = await proxy.local_store.execute(QueryPathInfoRequest(path=path))
        if not info_resp.valid or info_resp.info is None:
            cls._log.warning("nar_not_in_local_store", path=path)
            return NarFromPathResponse(nar_data=b"")

        nar_size = info_resp.info.nar_size

        op_log("NarFromPath").debug(
            "nar_from_path_streaming",
            path=path,
            size=nar_size,
        )

        await proxy.client.flush()
        proxy.w.write_uint64(wire.STDERR_LAST)

        async with proxy.local_store.transfer_conn() as conn:
            conn.w.write_uint64(Op.NarFromPath)
            await SingleStringRequest(path=path).to_writer(conn.w, conn.version)
            await conn.w.drain()
            await conn.r.drain_stderr()

            if nar_size > 0:
                from ..wire import _CHUNK_SIZE

                remaining = nar_size
                while remaining > 0:
                    to_read = min(remaining, _CHUNK_SIZE)
                    chunk = await conn.r.readexactly(to_read)
                    proxy.w.write(chunk)
                    remaining -= to_read
            else:
                await wire.stream_parse_nar(conn.r, proxy.w)

        await proxy.w.drain()
        return None


# ── QueryAllValidPaths ───────────────────────────────────────────────


@dataclass
class QueryAllValidPathsRequest(EmptyRequest[StringSetResponse]):
    op: ClassVar[int] = Op.QueryAllValidPaths
    response_type: ClassVar[type[OpResponse]] = StringSetResponse
    is_query: ClassVar[bool] = True

    async def execute_db(self, db: LocalStoreDB) -> StringSetResponse | None:
        async with db.acquire_conn() as conn:
            async with conn.execute(QUERY_ALL_VALID_PATHS) as cursor:
                rows = await cursor.fetchall()
        return StringSetResponse(paths={StorePath(r[0]) for r in rows})

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> StringSetResponse:
        try:
            if store.db:
                result = await store.db.execute(self)
                if result is not None:
                    store.add_known_paths(result.paths, update_regtime=False)
                    return result

            resp = await super().execute(store, client, suppress_last)
            store.add_known_paths(resp.paths, update_regtime=False)
            self._log.info(
                "sync_paths_complete", store_id=store.id, count=len(resp.paths)
            )
            return resp
        except Exception:
            self._log.warning("sync_paths_failed", store_id=store.id)
            store.known_paths = set()
            return StringSetResponse(paths=set())


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
        resp = await super().execute(store, client, suppress_last)

        if store.store_path:
            for dp in self.derived_paths:
                outputs = dp.to_outputs(store.store_path)
                store.add_known_paths(outputs)

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
