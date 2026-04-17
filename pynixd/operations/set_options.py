"""SetOptions operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from .. import wire
from ..wire import NixReader, NixWriter
from .base import (
    OpRequest,
    OpResponse,
    OperationLogs,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store

# Silence SetOptions by default — it's extremely verbose
# logging.getLogger("pynixd.operations.SetOptionsRequest").setLevel(logging.WARNING)


@dataclass
class SetOptionsResponse(OpResponse):
    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(reader)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer")
        self.logs.to_writer(writer)


@dataclass
class SetOptionsRequest(OpRequest[SetOptionsResponse]):
    name: ClassVar[str] = "SetOptions"
    op: ClassVar[int] = 19
    response_type: ClassVar[type[OpResponse]] = SetOptionsResponse
    keep_failed: int = 0
    keep_going: int = 0
    try_fallback: int = 0
    verbosity: int = 0
    max_build_jobs: int = 0
    max_silent_time: int = 0
    _obsolete_use_build_hook: int = 0
    build_verbosity: int = 0
    _obsolete_log_type: int = 0
    _obsolete_print_build_trace: int = 0
    build_cores: int = 0
    use_substitutes: int = 0
    overrides: dict[str, str] = field(default_factory=dict)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.keep_failed = await reader.read_uint64()
        self.keep_going = await reader.read_uint64()
        self.try_fallback = await reader.read_uint64()
        self.verbosity = await reader.read_uint64()
        self.max_build_jobs = await reader.read_uint64()
        self.max_silent_time = await reader.read_uint64()
        self._obsolete_use_build_hook = await reader.read_uint64()
        self.build_verbosity = await reader.read_uint64()
        self._obsolete_log_type = await reader.read_uint64()
        self._obsolete_print_build_trace = await reader.read_uint64()
        self.build_cores = await reader.read_uint64()
        self.use_substitutes = await reader.read_uint64()

        self.overrides = {}
        if version >= wire.proto(1, 12):
            n = await reader.read_uint64()
            for _ in range(n):
                k = await reader.read_string()
                v = await reader.read_string()
                self.overrides[k] = v

        self.logger.debug(
            "from_reader",
            keep_failed=self.keep_failed,
            keep_going=self.keep_going,
            try_fallback=self.try_fallback,
            verbosity=self.verbosity,
            max_build_jobs=self.max_build_jobs,
            max_silent_time=self.max_silent_time,
            build_verbosity=self.build_verbosity,
            build_cores=self.build_cores,
            use_substitutes=self.use_substitutes,
        )
        return self

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> SetOptionsResponse:
        # We don't forward SetOptions to the local store daemon as it would
        # mess with our own proxy's session state if it was a real daemon.
        # We just return SetOptionsResponse.
        return SetOptionsResponse()

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
