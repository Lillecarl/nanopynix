"""SetOptions operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..stderr import StderrNext
from ..types.auth import Role
from ..types.context import ReadContext
from ..types.protocol import Verbosity
from .base import OperationLogs, OpRequest, OpResponse

if TYPE_CHECKING:
    from ..types import RequestContext as RequestContext
    from ..types.context import WriteContext

# Silence SetOptions by default — it's extremely verbose


@dataclass
class SetOptionsResponse(OpResponse):
    # ── New-style API (ReadContext / WriteContext) ──────────────

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = await OperationLogs.deserialize(ctx)
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        self.logger.debug("serialize")
        self.logs.serialize(ctx)


@dataclass(kw_only=True)
class SetOptionsRequest(OpRequest[SetOptionsResponse]):
    name: ClassVar[str] = "SetOptions"
    op: ClassVar[int] = 19
    response_type: ClassVar[type[OpResponse]] = SetOptionsResponse
    keep_failed: int
    keep_going: int
    try_fallback: int
    verbosity: Verbosity
    max_build_jobs: int
    max_silent_time: int
    _obsolete_use_build_hook: int
    build_verbosity: Verbosity
    _obsolete_log_type: int
    _obsolete_print_build_trace: int
    build_cores: int
    use_substitutes: int
    overrides: dict[str, str]

    async def handle(self, ctx: RequestContext) -> SetOptionsResponse | None:
        r_ctx = ReadContext(reader=ctx.proxy.r, version=ctx.version)
        self = await self.deserialize(r_ctx)
        if ctx.proxy.role == Role.ADMIN:
            return await ctx.proxy.execute(self)

        resp = SetOptionsResponse()
        msg = StderrNext("pynixd: SetOptions ignored (no-op)")
        resp.logs.add(msg)
        return resp

    # ── New-style API (ReadContext / WriteContext) ──────────────

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.keep_failed = await ctx.reader.read_uint64()
        obj.keep_going = await ctx.reader.read_uint64()
        obj.try_fallback = await ctx.reader.read_uint64()
        obj.verbosity = Verbosity(await ctx.reader.read_uint64())
        obj.max_build_jobs = await ctx.reader.read_uint64()
        obj.max_silent_time = await ctx.reader.read_uint64()
        obj._obsolete_use_build_hook = await ctx.reader.read_uint64()
        obj.build_verbosity = Verbosity(await ctx.reader.read_uint64())
        obj._obsolete_log_type = await ctx.reader.read_uint64()
        obj._obsolete_print_build_trace = await ctx.reader.read_uint64()
        obj.build_cores = await ctx.reader.read_uint64()
        obj.use_substitutes = await ctx.reader.read_uint64()

        obj.overrides = {}
        if ctx.version >= wire.proto(1, 12):
            n = await ctx.reader.read_uint64()
            for _ in range(n):
                k = await ctx.reader.read_string()
                v = await ctx.reader.read_string()
                obj.overrides[k] = v

        obj.logger.debug(
            "deserialize",
            keep_failed=obj.keep_failed,
            keep_going=obj.keep_going,
            try_fallback=obj.try_fallback,
            verbosity=obj.verbosity,
            max_build_jobs=obj.max_build_jobs,
            max_silent_time=obj.max_silent_time,
            build_verbosity=obj.build_verbosity,
            build_cores=obj.build_cores,
            use_substitutes=obj.use_substitutes,
        )
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)
        ctx.writer.write_uint64(self.op)
        ctx.writer.write_uint64(self.keep_failed)
        ctx.writer.write_uint64(self.keep_going)
        ctx.writer.write_uint64(self.try_fallback)
        ctx.writer.write_uint64(self.verbosity)
        ctx.writer.write_uint64(self.max_build_jobs)
        ctx.writer.write_uint64(self.max_silent_time)
        ctx.writer.write_uint64(self._obsolete_use_build_hook)
        ctx.writer.write_uint64(self.build_verbosity)
        ctx.writer.write_uint64(self._obsolete_log_type)
        ctx.writer.write_uint64(self._obsolete_print_build_trace)
        ctx.writer.write_uint64(self.build_cores)
        ctx.writer.write_uint64(self.use_substitutes)
        if ctx.version >= wire.proto(1, 12):
            ctx.writer.write_uint64(len(self.overrides))
            for k, v in self.overrides.items():
                ctx.writer.write_string(k)
                ctx.writer.write_string(v)
