"""QueryMissing operation request/response types."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..derived_path import (
    DerivedPath as DerivedPath,
)
from ..derived_path import (
    OutputsNames as OutputsNames,
)
from ..stderr import OperationLogs
from ..store_path import StorePath
from ..substituter import (
    HttpBinaryCacheSubstituter as HttpBinaryCacheSubstituter,
)
from ..substituter import (
    SubstituterGroup as SubstituterGroup,
)
from ..substituter import (
    get_substituter_urls as get_substituter_urls,
)
from ..types.context import ReadContext
from .base import OpRequest, OpResponse
from .is_valid_path import IsValidPathRequest
from .query_derivation_output_map_batch import QueryDerivationOutputMapBatchRequest

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types import RequestContext
    from ..types.aliases import StorePathSet
    from ..types.context import WriteContext


@dataclass
class _QueryCtx:
    """Mutable execution context shared across swarm tasks."""

    sg: SubstituterGroup
    drv_to_wanted: dict[StorePath, set[str]]
    will_build: set[StorePath]
    will_substitute: set[StorePath]
    unknown: set[StorePath]
    store: Store
    seen: set[str]
    download_size: int = 0
    nar_size: int = 0


@dataclass
class QueryMissingResponse(OpResponse):
    will_build: StorePathSet
    will_substitute: StorePathSet
    unknown: StorePathSet
    download_size: int
    nar_size: int

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.deserialize(ctx)
        obj.will_build = await ctx.reader.read_string_set(StorePath)
        obj.will_substitute = await ctx.reader.read_string_set(StorePath)
        obj.unknown = await ctx.reader.read_string_set(StorePath)
        obj.download_size = await ctx.reader.read_uint64()
        obj.nar_size = await ctx.reader.read_uint64()
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug(
            "serialize",
            will_build=self.will_build,
            will_substitute=self.will_substitute,
            unknown=self.unknown,
        )
        self.logs.serialize(ctx)
        ctx.writer.write_string_set(self.will_build)
        ctx.writer.write_string_set(self.will_substitute)
        ctx.writer.write_string_set(self.unknown)
        ctx.writer.write_uint64(self.download_size)
        ctx.writer.write_uint64(self.nar_size)


@dataclass(kw_only=True)
class QueryMissingRequest(OpRequest[QueryMissingResponse]):
    name: ClassVar[str] = "QueryMissing"
    op: ClassVar[int] = 40
    response_type: ClassVar[type[OpResponse]] = QueryMissingResponse
    is_query: ClassVar[bool] = True
    derived_paths: set[DerivedPath]

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.derived_paths = await ctx.reader.read_string_set(DerivedPath)
        obj.logger.debug("deserialize", derived_paths=obj.derived_paths)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string_set(self.derived_paths)

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self.logger.debug("received_op")
        self = await self.deserialize(ReadContext.from_request(ctx))

        if ctx.proxy.substitution_manager is None:
            # Fall back to the old BFS execute() path
            return await self.execute(
                ctx.proxy.local_store,
                client=ctx.proxy.client,
            )

        self.logger.debug(
            "query_missing_goals",
            count=len(self.derived_paths),
        )
        return await ctx.proxy.goal_manager.query_paths(
            self.derived_paths,
            ctx.proxy.local_store,
            ctx.proxy.substitution_manager,
            scheduler=ctx.proxy.scheduler,
        )

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = True,
    ) -> QueryMissingResponse:
        if not self.derived_paths:
            return _empty_response()

        will_build: StorePathSet = set()
        will_substitute: StorePathSet = set()
        unknown: StorePathSet = set()
        seen: set[str] = set()
        drv_to_wanted: dict[StorePath, set[str]] = {}

        initial_paths: set[StorePath] = set()
        for dp in self.derived_paths:
            dp = dp.derived
            if dp.is_opaque:
                initial_paths.add(StorePath(dp.drv_path))
            elif dp.is_nested:
                continue
            else:
                drv = StorePath(dp.drv_path)
                initial_paths.add(drv)
                if isinstance(dp.outputs, OutputsNames):
                    drv_to_wanted.setdefault(drv, set()).update(dp.outputs.names)

        if not initial_paths:
            return _empty_response()

        subs = [HttpBinaryCacheSubstituter(url) for url in get_substituter_urls()]

        async with SubstituterGroup(subs) as sg:
            ctx = _QueryCtx(
                sg=sg,
                drv_to_wanted=drv_to_wanted,
                will_build=will_build,
                will_substitute=will_substitute,
                unknown=unknown,
                store=store,
                seen=seen,
            )

            async with asyncio.TaskGroup() as tg:
                sg.tg = tg
                for path in initial_paths:
                    sg.spawn(_resolve_path(path, store, client, suppress_last, ctx))

        return QueryMissingResponse(
            will_build=will_build,
            will_substitute=will_substitute,
            unknown=unknown,
            download_size=ctx.download_size,
            nar_size=ctx.nar_size,
        )


async def _resolve_path(
    path: StorePath,
    store: Store,
    client: ClientConn | None,
    suppress_last: bool,
    ctx: _QueryCtx,
) -> None:
    if str(path) in ctx.seen:
        return
    ctx.seen.add(str(path))

    is_local = (await IsValidPathRequest(path=path).execute(store, client, suppress_last)).valid

    if not is_local:
        info = await ctx.sg.has_path(path)
        if info is not None:
            ctx.will_substitute.add(path)
            ctx.download_size += info.download_size
            ctx.nar_size += info.nar_size
            if path.is_derivation():
                outputs = await _fetch_output_map(path, store, client, suppress_last)
                wanted = ctx.drv_to_wanted.get(path, set())
                for name, opath in outputs.items():
                    if opath is None or opath == StorePath(""):
                        continue
                    if wanted and name not in wanted:
                        continue
                    if opath in ctx.will_substitute:
                        continue
                    ctx.sg.spawn(_resolve_path(opath, store, client, suppress_last, ctx))
        else:
            ctx.unknown.add(path)
        return

    if not path.is_derivation():
        return

    outputs = await _fetch_output_map(path, store, client, suppress_last)
    wanted = ctx.drv_to_wanted.get(path, set())

    still_missing = False
    for name, opath in outputs.items():
        if wanted and name not in wanted:
            continue
        if opath is None or opath == StorePath(""):
            still_missing = True
            continue
        op_valid = (await IsValidPathRequest(path=opath).execute(store, client, suppress_last)).valid
        if op_valid:
            continue
        info = await ctx.sg.has_path(opath)
        if info is not None:
            ctx.will_substitute.add(opath)
            ctx.download_size += info.download_size
            ctx.nar_size += info.nar_size
        else:
            still_missing = True

    if not still_missing:
        return

    ctx.will_build.add(path)
    try:
        parsed = await ctx.store.read_derivation(path)
    except FileNotFoundError:
        ctx.unknown.add(path)
        return

    if parsed is None:
        ctx.unknown.add(path)
        return

    for input_drv in parsed.input_drvs:
        ctx.sg.spawn(_resolve_path(input_drv, store, client, suppress_last, ctx))


async def _fetch_output_map(
    drv: StorePath,
    store: Store,
    client: ClientConn | None,
    suppress_last: bool,
) -> dict[str, StorePath | None]:
    resp = await QueryDerivationOutputMapBatchRequest(
        drv_paths={drv},
    ).execute(store, client, suppress_last)
    return resp.outputs.get(drv, {})


def _empty_response() -> QueryMissingResponse:
    return QueryMissingResponse(
        will_build=set(),
        will_substitute=set(),
        unknown=set(),
        download_size=0,
        nar_size=0,
    )
