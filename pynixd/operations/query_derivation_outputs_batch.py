"""QueryDerivationOutputsBatch operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..drv_parser import read_drv_file
from ..exceptions import OpNotImplementedError
from ..store_path import StorePath
from .base import OpRequest, OpResponse

QUERY_DERIVATION_OUTPUTS_BATCH = """
SELECT vp_drv.path, do.id, do.path
FROM DerivationOutputs do
JOIN ValidPaths vp_drv ON do.drv = vp_drv.id
WHERE vp_drv.path IN (SELECT value FROM json_each(?))
"""

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..wire import NixReader, NixWriter


@dataclass
class DerivationOutputsBatchResponse(OpResponse):
    """{drv_path: {output_name: output_path}}."""

    outputs: dict[StorePath, dict[str, StorePath]] = field(default_factory=dict)

    @property
    def is_not_found(self) -> bool:
        return not self.outputs

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        await self.logs.from_reader(reader, client=client, buffer=buffer_logs)
        n = await reader.read_uint64()
        self.outputs = {}
        for _ in range(n):
            drv_path = await reader.read_string(StorePath)
            m = await reader.read_uint64()
            drv_outputs: dict[str, StorePath] = {}
            for _ in range(m):
                name = await reader.read_string()
                path = await reader.read_string(StorePath)
                drv_outputs[name] = path
            self.outputs[drv_path] = drv_outputs
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", drv_path_count=len(self.outputs))
        self.logs.to_writer(writer)
        writer.write_uint64(len(self.outputs))
        for drv_path, drv_outputs in self.outputs.items():
            writer.write_string(drv_path)
            writer.write_uint64(len(drv_outputs))
            for name, path in drv_outputs.items():
                writer.write_string(name)
                writer.write_string(path)


@dataclass
class QueryDerivationOutputsBatchRequest(OpRequest[DerivationOutputsBatchResponse]):
    name: ClassVar[str] = "QueryDerivationOutputsBatch"
    op: ClassVar[int] = 106
    is_extension: ClassVar[bool] = True
    response_type: ClassVar[type[OpResponse]] = DerivationOutputsBatchResponse
    is_query: ClassVar[bool] = True
    drv_paths: set[StorePath] = field(default_factory=set)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.drv_paths = await reader.read_string_set(StorePath)
        self.logger.debug("from_reader", drv_paths=self.drv_paths)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.drv_paths)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> DerivationOutputsBatchResponse:
        if not self.drv_paths:
            return DerivationOutputsBatchResponse(outputs={})

        if (db := store.native_db) is not None:
            paths_json = json.dumps([str(p) for p in self.drv_paths])
            async with db.execute(
                QUERY_DERIVATION_OUTPUTS_BATCH,
                (paths_json,),
            ) as cursor:
                rows = await cursor.fetchall()

            result: dict[StorePath, dict[str, StorePath]] = {}
            for drv_path, output_name, output_path in rows:
                result.setdefault(StorePath(drv_path), {})[output_name] = StorePath(
                    output_path,
                )
            return DerivationOutputsBatchResponse(outputs=result)

        # Try delegation via wire (if talking to another pynixd)
        try:
            return await super().execute(store, client, suppress_last)
        except OpNotImplementedError:
            pass  # Backend doesn't support the extension, fall back to local file reading

        outputs: dict[StorePath, dict[str, StorePath]] = {}
        for drv_path in self.drv_paths:
            try:
                parsed = read_drv_file(store.store_path, drv_path)
                outputs[drv_path] = parsed.output_paths()
            except FileNotFoundError:
                pass
        return DerivationOutputsBatchResponse(outputs=outputs)
