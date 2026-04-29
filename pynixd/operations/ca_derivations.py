"""
Content-Addressed derivation operation request/response types.

These operations handle CA derivations:
- RegisterDrvOutput (op 42): register a realised output for a derivation
- QueryRealisation (op 43): query the realisation of a derivation output

Protocol: 1.32+ only (simplified - no version branching needed)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..store_path import DrvOutput, StorePath
from .base import (
    OpRequest,
    OpResponse,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.ca import Realisation
    from ..wire import NixReader, NixWriter


# ── RegisterDrvOutput (op 42) ─────────────────────────────────────────


@dataclass
class RegisterDrvOutputResponse(OpResponse):
    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        await self.logs.from_reader(
            reader,
            client=client,
            buffer=buffer_logs,
        )
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer")
        self.logs.to_writer(writer)


@dataclass
class RegisterDrvOutputRequest(OpRequest[RegisterDrvOutputResponse]):
    """Request to register a realised derivation output.

    Input: Realisation JSON string
    """

    name: ClassVar[str] = "RegisterDrvOutput"
    op: ClassVar[int] = 42
    response_type: ClassVar[type[OpResponse]] = RegisterDrvOutputResponse
    realisation: Realisation = field(default_factory=dict)  # type: ignore[arg-type]

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        realisation_json = await reader.read_string()
        self.realisation = json.loads(realisation_json)
        self.logger.debug("from_reader", realisation=self.realisation)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(json.dumps(self.realisation))

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> RegisterDrvOutputResponse:
        resp = await store.call(self, client=client, suppress_last=suppress_last)

        out_path = self.realisation.get("outPath")
        if out_path:
            store.tracker.add_known_path(StorePath(out_path).with_store_prefix())

        return resp


# ── QueryRealisation (op 43) ───────────────────────────────────────────


@dataclass
class QueryRealisationResponse(OpResponse):
    """Response to query the realisation of a derivation output.

    Output: Set of Realisations (JSON dicts)
    """

    realisations: list[dict] = field(default_factory=list)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        await self.logs.from_reader(
            reader,
            client=client,
            buffer=buffer_logs,
        )
        n = await reader.read_uint64()
        self.realisations = []
        for _ in range(n):
            realisation_json = await reader.read_string()
            self.realisations.append(json.loads(realisation_json))
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", realisation_count=len(self.realisations))
        self.logs.to_writer(writer)
        writer.write_uint64(len(self.realisations))
        for r in self.realisations:
            writer.write_string(json.dumps(r))


@dataclass
class QueryRealisationRequest(OpRequest[QueryRealisationResponse]):
    """Request to query the realisation of a derivation output."""

    name: ClassVar[str] = "QueryRealisation"
    op: ClassVar[int] = 43
    response_type: ClassVar[type[OpResponse]] = QueryRealisationResponse
    is_query: ClassVar[bool] = True
    drv_output: DrvOutput = field(default_factory=DrvOutput)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        raw = await reader.read_string()
        self.drv_output = DrvOutput(raw)
        self.logger.debug("from_reader", drv_output=self.drv_output)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.drv_output)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> QueryRealisationResponse:
        resp = await store.call(self, client=client, suppress_last=suppress_last)

        for r in resp.realisations:
            out_path = r.get("outPath")
            if out_path:
                store.tracker.add_known_path(StorePath(out_path).with_store_prefix())

        return resp
