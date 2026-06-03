"""QueryMissing operation request/response types."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..derived_path import (
    DerivedPath as DerivedPath,
)
from ..derived_path import (
    DerivedPathBuilt as DerivedPathBuilt,
)
from ..derived_path import (
    DerivedPathOpaque as DerivedPathOpaque,
)
from ..derived_path import (
    OutputsNames as OutputsNames,
)
from ..derived_path import (
    SingleDerivedPathBuilt as SingleDerivedPathBuilt,
)
from ..drv_parser import read_drv_file
from ..stderr import OperationLogs
from ..store_path import StorePath
from ..substituter import (
    HttpBinaryCacheSubstituter as HttpBinaryCacheSubstituter,
)
from ..substituter import (
    get_substituter_urls as get_substituter_urls,
)
from .base import OpRequest, OpResponse
from .query_derivation_output_map_batch import QueryDerivationOutputMapBatchRequest
from .query_valid_paths import QueryValidPathsRequest

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.aliases import StorePathSet
    from ..types.context import ReadContext, WriteContext

_BATCH_SIZE = 256


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

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = True,
    ) -> QueryMissingResponse:
        """Execute QueryMissing in-process with a self-feeding queue.

        One batch SQLite validity check per iteration.  Missing paths are
        checked against HTTP substituters and their .drv files in a
        single TaskGroup.  Valid drvs have their output paths checked and
        trigger will_build/will_substitute decisions.
        """
        if not self.derived_paths:
            return _empty_response()

        will_build: StorePathSet = set()
        will_substitute: StorePathSet = set()
        unknown: StorePathSet = set()
        seen: set[str] = set()
        pending: set[StorePath] = set()
        drv_to_wanted: dict[StorePath, set[str]] = {}

        for dp in self.derived_paths:
            derived = dp.derived
            if isinstance(derived, DerivedPathOpaque):
                pending.add(derived.path)
            elif isinstance(derived, DerivedPathBuilt):
                if isinstance(derived.drv_path, SingleDerivedPathBuilt):
                    continue
                drv = StorePath(derived.base_store_path())
                pending.add(drv)
                if isinstance(derived.outputs, OutputsNames):
                    drv_to_wanted[drv] = set(derived.outputs.names)

        if not pending:
            return _empty_response()

        subs = [HttpBinaryCacheSubstituter(url) for url in get_substituter_urls()]

        async with contextlib.AsyncExitStack() as stack:
            for sub in subs:
                await stack.enter_async_context(sub)

            while pending:
                unseen = {p for p in pending if str(p) not in seen}
                pending.clear()
                if not unseen:
                    break
                for p in unseen:
                    seen.add(str(p))

                valid = (
                    await QueryValidPathsRequest(
                        paths=unseen,
                        substitute=0,
                    ).execute(store, client, suppress_last)
                ).paths

                missing = unseen - valid

                drv_valid = {p for p in valid if p.is_derivation()}
                if drv_valid:
                    out_maps = (
                        await QueryDerivationOutputMapBatchRequest(
                            drv_paths=drv_valid,
                        ).execute(store, client, suppress_last)
                    ).outputs

                    all_output_paths: set[StorePath] = set()
                    for drv, outputs in out_maps.items():
                        wanted = drv_to_wanted.get(drv, set())
                        for name, opath in outputs.items():
                            if opath is None:
                                continue
                            if wanted and name not in wanted:
                                continue
                            all_output_paths.add(opath)

                    valid_outputs: StorePathSet = set()
                    if all_output_paths:
                        valid_outputs = (
                            await QueryValidPathsRequest(
                                paths=all_output_paths,
                                substitute=0,
                            ).execute(store, client, suppress_last)
                        ).paths

                    for drv, outputs in out_maps.items():
                        wanted = drv_to_wanted.get(drv, set())
                        invalid_outputs: set[StorePath] = set()
                        for name, opath in outputs.items():
                            if opath is None:
                                continue
                            if wanted and name not in wanted:
                                continue
                            if opath not in valid_outputs:
                                invalid_outputs.add(opath)

                        if not invalid_outputs:
                            continue

                        for sub in subs:
                            if not invalid_outputs:
                                break
                            found = await sub.query_substitutable_paths(invalid_outputs)
                            will_substitute |= found
                            invalid_outputs -= found

                        if invalid_outputs:
                            will_build.add(drv)
                            try:
                                parsed = await read_drv_file(store.store_path, drv)
                            except FileNotFoundError:
                                unknown.add(drv)
                                continue
                            for input_drv in parsed.input_drvs:
                                if str(input_drv) not in seen:
                                    pending.add(input_drv)

                if not missing:
                    continue

                drv_missing = {p for p in missing if p.is_derivation()}

                async with asyncio.TaskGroup() as tg:
                    for sub in subs:
                        tg.create_task(
                            _query_substituter(sub, missing, will_substitute),
                        )
                    for drv in drv_missing:
                        tg.create_task(
                            _read_and_enqueue(
                                drv,
                                store,
                                will_build,
                                will_substitute,
                                unknown,
                                pending,
                                seen,
                            ),
                        )

                missing -= will_substitute
                unknown |= {p for p in missing if not p.is_derivation()}

        return QueryMissingResponse(
            will_build=will_build,
            will_substitute=will_substitute,
            unknown=unknown,
            download_size=0,
            nar_size=0,
        )


def _empty_response() -> QueryMissingResponse:
    return QueryMissingResponse(
        will_build=set(),
        will_substitute=set(),
        unknown=set(),
        download_size=0,
        nar_size=0,
    )


async def _query_substituter(
    sub: HttpBinaryCacheSubstituter,
    paths: set[StorePath],
    will_substitute: StorePathSet,
) -> None:
    found = await sub.query_substitutable_paths(paths)
    will_substitute |= found


async def _read_and_enqueue(
    drv: StorePath,
    store: Store,
    will_build: StorePathSet,
    will_substitute: StorePathSet,
    unknown: StorePathSet,
    pending: set[StorePath],
    seen: set[str],
) -> None:
    if drv in will_substitute:
        return
    try:
        parsed = await read_drv_file(store.store_path, drv)
    except FileNotFoundError:
        unknown.add(drv)
        return
    will_build.add(drv)
    for input_drv in parsed.input_drvs:
        if str(input_drv) not in seen:
            pending.add(input_drv)
