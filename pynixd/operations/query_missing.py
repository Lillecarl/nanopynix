"""QueryMissing operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..derived_path import DerivedPath
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


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
    name: ClassVar[str] = "QueryMissing"
    op: ClassVar[int] = 40
    response_type: ClassVar[type[OpResponse]] = QueryMissingResponse
    is_query: ClassVar[bool] = True
    derived_paths: set[DerivedPath] = field(default_factory=set)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(derived_paths=await reader.read_string_set(DerivedPath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
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
                    from .query_valid_paths import QueryValidPathsRequest

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
