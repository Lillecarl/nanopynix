"""QueryDerivationOutputMapBatch extension (op 106, pynixd-internal).

Batch reads derivation output maps from the local SQLite DerivationOutputs table
or falls back to parsing .drv files.  This is NOT the deprecated wire protocol
op 22 (QueryDerivationOutputs) — that op is not implemented in pynixd;
pynixd uses QueryDerivationOutputMap (op 41) instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..drv_parser import read_drv_file
from ..exceptions import OpNotImplementedError
from ..stderr import OperationLogs
from ..store_path import StorePath
from .base import OpRequest, OpResponse

QUERY_DERIVATION_OUTPUT_MAP_BATCH = """
SELECT vp_drv.path, do.id, do.path
FROM DerivationOutputs do
JOIN ValidPaths vp_drv ON do.drv = vp_drv.id
WHERE vp_drv.path IN (SELECT value FROM json_each(?))
"""

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.aliases import OutputMap, StorePathSet
    from ..wire import NixReader, NixWriter


@dataclass
class DerivationOutputMapBatchResponse(OpResponse):
    """{drv_path: {output_name: output_path_or_none}}.

    Output paths can be None when the output hasn't been realised yet.
    """

    outputs: OutputMap

    @property
    def is_not_found(self) -> bool:
        return not self.outputs

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.logs = OperationLogs()
        await obj.logs.from_reader(reader, client=client, buffer=buffer_logs)
        n = await reader.read_uint64()
        obj.outputs = {}
        for _ in range(n):
            drv_path = await reader.read_string(StorePath)
            m = await reader.read_uint64()
            drv_outputs: dict[str, StorePath | None] = {}
            for _ in range(m):
                name = await reader.read_string()
                path = await reader.read_string(StorePath)
                drv_outputs[name] = path or None
            obj.outputs[drv_path] = drv_outputs
        return obj

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
                if path is not None:
                    writer.write_string(path)
                else:
                    writer.write_string("")


@dataclass(kw_only=True)
class QueryDerivationOutputMapBatchRequest(OpRequest[DerivationOutputMapBatchResponse]):
    name: ClassVar[str] = "QueryDerivationOutputMapBatch"
    op: ClassVar[int] = 106
    is_extension: ClassVar[bool] = True
    response_type: ClassVar[type[OpResponse]] = DerivationOutputMapBatchResponse
    is_query: ClassVar[bool] = True
    drv_paths: StorePathSet

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,  # noqa: ARG003
    ) -> Self:
        obj = cls.__new__(cls)
        obj.drv_paths = await reader.read_string_set(StorePath)
        obj.logger.debug("from_reader", drv_paths=obj.drv_paths)
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string_set(self.drv_paths)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> DerivationOutputMapBatchResponse:
        if not self.drv_paths:
            return DerivationOutputMapBatchResponse({})

        if (db := store.db) is not None:
            paths_json = json.dumps([str(p) for p in self.drv_paths])
            async with db.execute(
                QUERY_DERIVATION_OUTPUT_MAP_BATCH,
                (paths_json,),
            ) as cursor:
                rows = await cursor.fetchall()

            result: OutputMap = {}
            for drv_path, output_name, output_path in rows:
                result.setdefault(StorePath(drv_path), {})[output_name] = (
                    StorePath(output_path) if output_path else None
                )
            return DerivationOutputMapBatchResponse(outputs=result)

        # Try delegation via wire (if talking to another pynixd)
        try:
            return await super().execute(store, client, suppress_last)
        except OpNotImplementedError:
            pass  # Backend doesn't support the extension, fall back to local file reading

        outputs: OutputMap = {}
        for drv_path in self.drv_paths:
            try:
                parsed = await read_drv_file(store.store_path, drv_path)
                out = parsed.output_paths()
                outputs[drv_path] = dict(out.items())
            except FileNotFoundError:
                pass
        return DerivationOutputMapBatchResponse(outputs=outputs)
