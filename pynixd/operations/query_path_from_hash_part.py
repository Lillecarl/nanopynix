"""QueryPathFromHashPart operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..protocol import Op
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse

QUERY_PATH_FROM_HASH_PART = """
SELECT path FROM ValidPaths WHERE path >= ? AND path < ? LIMIT 1
"""

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store


@dataclass
class QueryPathFromHashPartResponse(OpResponse):
    value: StorePath = field(default_factory=lambda: StorePath(""))

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(value=await reader.read_string(StorePath))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.value)


@dataclass
class QueryPathFromHashPartRequest(OpRequest[QueryPathFromHashPartResponse]):
    op: ClassVar[int] = Op.QueryPathFromHashPart
    response_type: ClassVar[type[OpResponse]] = QueryPathFromHashPartResponse
    is_query: ClassVar[bool] = True
    path: str = ""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(path=await reader.read_string())

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.path)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryPathFromHashPartResponse:
        if store.db is not None:
            prefix = f"/nix/store/{self.path}"
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            async with store.db.execute(
                QUERY_PATH_FROM_HASH_PART, (prefix, upper)
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                result = QueryPathFromHashPartResponse(value=StorePath(row[0]))
                store.add_known_path(result.value)
                return result

        resp = await store.call(self, client=client, suppress_last=suppress_last)
        if resp.value:
            store.add_known_path(StorePath(resp.value))
        return resp
