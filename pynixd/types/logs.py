"""Operation logging and stderr buffering domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

from .. import constants
from ..exceptions import BackendError
from ..stderr import StderrError, read_stream

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..stderr import StderrMsg
    from ..wire import NixReader, NixWriter


@dataclass
class OperationLogs:
    """Container for stderr messages from an operation."""

    messages: list[StderrMsg] = field(default_factory=list)

    @property
    def error(self):
        for msg in self.messages:
            if isinstance(msg, StderrError):
                return msg
        return None

    @property
    def has_error(self) -> bool:
        return self.error is not None

    def __bool__(self) -> bool:
        return not self.has_error

    def add(self, msg: StderrMsg) -> None:
        self.messages.append(msg)

    def to_writer(self, writer: NixWriter) -> None:
        for msg in self.messages:
            msg.to_writer(writer)
        writer.write_uint64(constants.STDERR_LAST)

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,
        client: ClientConn | None = None,
        buffer: bool = True,
    ) -> Self:
        obj = cls.__new__(cls)
        obj.messages = []
        async for msg in read_stream(reader):
            if client:
                await client.queue.put(msg)
            if buffer:
                obj.add(msg)
            if isinstance(msg, StderrError):
                raise BackendError(f"Daemon error ({msg.error_type}): {msg.msg}")
        return obj
