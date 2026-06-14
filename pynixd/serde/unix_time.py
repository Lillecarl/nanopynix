from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .wire_message import WireModel

if TYPE_CHECKING:
    from ..types.context import ReadContext, WriteContext


class Time(WireModel):
    """Unix timestamp in seconds on the wire. Exposes as datetime property."""

    ts: int = 0

    @classmethod
    async def from_reader(cls, ctx: ReadContext):
        return cls.model_construct(ts=await ctx.reader.read_uint64())

    async def to_writer(self, ctx: WriteContext) -> None:
        ctx.writer.write_uint64(self.ts)

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.ts, tz=UTC)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Time):
            return self.ts == other.ts
        if isinstance(other, int):
            return self.ts == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.ts)
