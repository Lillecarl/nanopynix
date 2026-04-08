"""QueryDerivationOutputsBatch operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..exceptions import OpNotImplementedError
from ..protocol import Op
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import OpRequest, OpResponse

QUERY_DERIVATION_OUTPUTS_BATCH = """
SELECT vp_drv.path, do.id, do.path
FROM DerivationOutputs do
JOIN ValidPaths vp_drv ON do.drv = vp_drv.id
WHERE vp_drv.path IN (SELECT value FROM json_each(?))
"""

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..local_store_db import LocalStoreDB
    from ..proxy import DaemonProxy
    from ..store import Store

log = structlog.get_logger(__name__)


@dataclass
class DerivationOutputsBatchResponse(OpResponse):
    """{drv_path: {output_name: output_path}}."""

    outputs: dict[StorePath, dict[str, StorePath]] = field(default_factory=dict)

    @property
    def is_not_found(self) -> bool:
        return not self.outputs

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
        writer.write_uint64(self.op)
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
        try:
            result = await super().execute(store, client, suppress_last)
            if not result.is_not_found:
                return result
        except OpNotImplementedError:
            pass

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

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> DerivationOutputsBatchResponse:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        return await proxy.execute(request)
