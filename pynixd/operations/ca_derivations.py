"""
Content-Addressed derivation operation request/response types.

These operations handle CA derivations:
- RegisterDrvOutput (op 42): register a realised output for a derivation
- QueryRealisation (op 43): query the realisation of a derivation output

Protocol: 1.32+ only (simplified - no version branching needed)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import OperationLogs
from ..store_path import DrvOutput, StorePath
from .base import OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.ca import Realisation
    from ..types.context import ReadContext, WriteContext


# ── RegisterDrvOutput (op 42) ─────────────────────────────────────────


@dataclass
class RegisterDrvOutputResponse(OpResponse):
    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logs.serialize(ctx)


@dataclass(kw_only=True)
class RegisterDrvOutputRequest(OpRequest[RegisterDrvOutputResponse]):
    """Request to register a realised derivation output.

    Input: Realisation JSON string
    """

    name: ClassVar[str] = "RegisterDrvOutput"
    op: ClassVar[int] = 42
    response_type: ClassVar[type[OpResponse]] = RegisterDrvOutputResponse
    realisation: Realisation

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        realisation_json = await ctx.reader.read_string()
        obj.realisation = json.loads(realisation_json)
        obj.logger.debug("deserialize", realisation=obj.realisation)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string(json.dumps(self.realisation))

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

    realisations: list[dict]

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        n = await ctx.reader.read_uint64()
        obj.realisations = []
        for _ in range(n):
            realisation_json = await ctx.reader.read_string()
            obj.realisations.append(json.loads(realisation_json))
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logs.serialize(ctx)
        ctx.writer.write_uint64(len(self.realisations))
        for r in self.realisations:
            ctx.writer.write_string(json.dumps(r))


@dataclass(kw_only=True)
class QueryRealisationRequest(OpRequest[QueryRealisationResponse]):
    """Request to query the realisation of a derivation output."""

    name: ClassVar[str] = "QueryRealisation"
    op: ClassVar[int] = 43
    response_type: ClassVar[type[OpResponse]] = QueryRealisationResponse
    is_query: ClassVar[bool] = True
    drv_output: DrvOutput

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        raw = await ctx.reader.read_string()
        obj.drv_output = DrvOutput(raw)
        obj.logger.debug("deserialize", drv_output=obj.drv_output)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_string(self.drv_output)

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
