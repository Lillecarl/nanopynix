"""QueryDerivationOutputsBatch operation request/response types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..exceptions import OpNotImplementedError
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from ..drv_parser import read_drv_file
from .base import OpRequest, OpResponse, OperationLogs

QUERY_DERIVATION_OUTPUTS_BATCH = """
SELECT vp_drv.path, do.id, do.path
FROM DerivationOutputs do
JOIN ValidPaths vp_drv ON do.drv = vp_drv.id
WHERE vp_drv.path IN (SELECT value FROM json_each(?))
"""

if TYPE_CHECKING:
    from ..connection import ClientConn
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
        logs = await OperationLogs.from_reader(reader)
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
        return cls(logs=logs, outputs=outputs)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
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

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        drv_paths = await reader.read_string_set(StorePath)
        cls.logger.debug("from_reader", drv_paths=drv_paths)
        return cls(drv_paths=drv_paths)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
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

        if store.db is not None:
            paths_json = json.dumps([str(p) for p in self.drv_paths])
            async with store.db.execute(
                QUERY_DERIVATION_OUTPUTS_BATCH, (paths_json,)
            ) as cursor:
                rows = await cursor.fetchall()

            result: dict[StorePath, dict[str, StorePath]] = {}
            for drv_path, output_name, output_path in rows:
                result.setdefault(StorePath(drv_path), {})[output_name] = StorePath(
                    output_path
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

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> DerivationOutputsBatchResponse:
        log = structlog.get_logger(f"pynixd.operations.{cls.__name__}")
        log.debug("received_op")
        request = await cls.from_reader(proxy.r, proxy.version)
        result = await proxy.execute(request)
        log.debug("responded_op")
        return result
