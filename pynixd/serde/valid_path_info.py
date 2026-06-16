"""ValidPathInfo — StorePath + UnkeyedValidPathInfo as nested fields."""

from __future__ import annotations

from ..types.context import WriteContext
from .path_info import UnkeyedValidPathInfo  # noqa: TC001
from .store_path import StorePath  # noqa: TC001
from .wire_message import WireModel


class ValidPathInfo(WireModel):
    """Wire mirror of ValidPathInfo.

    Wire order: ``path`` then all ``info`` fields inline.
    ``WireModel`` serializes nested ``WireModel`` fields inline,
    so this produces the correct flat wire format.
    """

    path: StorePath
    info: UnkeyedValidPathInfo

    async def bytes_wire(self) -> bytes:
        """Serialize this ValidPathInfo to bytes (in-memory BytesWriter is sync)."""
        from ..wire import BytesWriter

        buf = BytesWriter()
        ctx = WriteContext(writer=buf, version=0)
        await self.to_writer(ctx)
        return buf.get_bytes()
