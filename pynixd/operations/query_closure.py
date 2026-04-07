"""QueryClosure operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..protocol import Op
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..proxy import DaemonProxy
    from ..store import Store

log = structlog.get_logger(__name__)


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
        from .query_closure_with_info import QueryClosureWithInfoRequest

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
