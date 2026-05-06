"""SetOptions operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..stderr import StderrNext
from ..types.auth import Role
from ..types.protocol import Verbosity
from .base import OperationLogs, OpRequest, OpResponse

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..types import RequestContext as RequestContext
    from ..wire import NixReader, NixWriter

# Silence SetOptions by default — it's extremely verbose


@dataclass
class SetOptionsResponse(OpResponse):
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
        await obj.logs.from_reader(
            reader,
            client=client,
            buffer=buffer_logs,
        )
        return obj

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer")
        self.logs.to_writer(writer)


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

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,  # noqa: ARG003
        buffer_logs: bool = True,  # noqa: ARG003
    ) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=reader.identifier)
        obj.keep_failed = await reader.read_uint64()
        obj.keep_going = await reader.read_uint64()
        obj.try_fallback = await reader.read_uint64()
        obj.verbosity = Verbosity(await reader.read_uint64())
        obj.max_build_jobs = await reader.read_uint64()
        obj.max_silent_time = await reader.read_uint64()
        obj._obsolete_use_build_hook = await reader.read_uint64()
        obj.build_verbosity = Verbosity(await reader.read_uint64())
        obj._obsolete_log_type = await reader.read_uint64()
        obj._obsolete_print_build_trace = await reader.read_uint64()
        obj.build_cores = await reader.read_uint64()
        obj.use_substitutes = await reader.read_uint64()

        obj.overrides = {}
        if version >= wire.proto(1, 12):
            n = await reader.read_uint64()
            for _ in range(n):
                k = await reader.read_string()
                v = await reader.read_string()
                obj.overrides[k] = v

        obj.logger.debug(
            "from_reader",
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

    async def handle(self, ctx: RequestContext) -> SetOptionsResponse | None:
        self = await self.from_reader(ctx.proxy.r, ctx.version)
        if ctx.proxy.role == Role.ADMIN:
            return await ctx.proxy.execute(self)

        resp = SetOptionsResponse()
        msg = StderrNext("pynixd: SetOptions ignored (no-op)")
        resp.logs.add(msg)
        return resp

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_uint64(self.keep_failed)
        writer.write_uint64(self.keep_going)
        writer.write_uint64(self.try_fallback)
        writer.write_uint64(self.verbosity)
        writer.write_uint64(self.max_build_jobs)
        writer.write_uint64(self.max_silent_time)
        writer.write_uint64(self._obsolete_use_build_hook)
        writer.write_uint64(self.build_verbosity)
        writer.write_uint64(self._obsolete_log_type)
        writer.write_uint64(self._obsolete_print_build_trace)
        writer.write_uint64(self.build_cores)
        writer.write_uint64(self.use_substitutes)
        if version >= wire.proto(1, 12):
            writer.write_uint64(len(self.overrides))
            for k, v in self.overrides.items():
                writer.write_string(k)
                writer.write_string(v)
