"""IsValidPath operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..protocol import Op
from ..wire import NixReader, NixWriter
from .base import OpResponse, SingleStringRequest

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..local_store_db import LocalStoreDB
    from ..store import Store

IS_VALID_PATH = "SELECT 1 FROM ValidPaths WHERE path = ? LIMIT 1"


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

        resp = await super().execute(store, client, suppress_last)
        if resp.valid:
            store.add_known_path(self.path)
        return resp
