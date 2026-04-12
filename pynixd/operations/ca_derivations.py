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
from typing import ClassVar, Self

from ..wire import NixReader, NixWriter
from .base import (
    OpRequest,
    OpResponse,
    OperationLogs,
)

# ── DrvOutput ──────────────────────────────────────────────────────────

# DrvOutput is a string of the form "hash:name" identifying a derivation output
DrvOutput = str


# ── RegisterDrvOutput (op 42) ─────────────────────────────────────────


@dataclass
class RegisterDrvOutputResponse(OpResponse):
    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        logs = await OperationLogs.from_reader(reader)
        return cls(logs=logs)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
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
    realisation: dict = field(default_factory=dict)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        realisation_json = await reader.read_string()
        realisation = json.loads(realisation_json)
        cls.logger.debug("from_reader", realisation=realisation)
        return cls(realisation=realisation)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(json.dumps(self.realisation))


# ── QueryRealisation (op 43) ───────────────────────────────────────────


@dataclass
class QueryRealisationResponse(OpResponse):
    """Response to query the realisation of a derivation output.

    Output: Set of Realisations (JSON dicts)
    """

    realisations: list[dict] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        logs = await OperationLogs.from_reader(reader)
        n = await reader.read_uint64()
        realisations = []
        for _ in range(n):
            realisation_json = await reader.read_string()
            realisations.append(json.loads(realisation_json))
        return cls(logs=logs, realisations=realisations)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
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
    drv_output: DrvOutput = ""

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        drv_output = await reader.read_string()
        cls.logger.debug("from_reader", drv_output=drv_output)
        return cls(drv_output=drv_output)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        writer.write_string(self.drv_output)
