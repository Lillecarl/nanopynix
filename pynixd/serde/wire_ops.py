"""WireRequest / WireResponse — base classes for Nix daemon operations.

These live in their own module to avoid circular imports: they depend on
both ``wire_message`` (WireModel, WireField) and ``logs`` (WireLogs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import Field as PydanticField

from .logs import WireLogs
from .wire_message import WireModel

if TYPE_CHECKING:
    from ..types.context import ReadContext, WriteContext


class WireRequest(WireModel):
    """Base class for Nix daemon protocol requests.

    Subclasses must override ``op`` and ``response_type``::

        class SomeRequest(WireRequest):
            op: ClassVar[int] = 42
            response_type: ClassVar[type[SomeResponse]] = SomeResponse
            path: StorePath
    """

    op: ClassVar[int]
    response_type: ClassVar[type]
    forward: ClassVar[bool] = True
    is_extension: ClassVar[bool] = False

    async def to_writer(self, ctx: WriteContext) -> None:
        """Write op code then body."""
        ctx.writer.write_uint64(self.op)
        await super().to_writer(ctx)

    @classmethod
    async def from_reader(cls, ctx: ReadContext):
        """Read body only — op was consumed by dispatch."""
        return await super().from_reader(ctx)


class WireResponse(WireModel):
    """Base class for Nix daemon protocol responses.

    The ``logs`` field is a ``WireLogs`` (stderr stream).  Because it is a
    ``WireModel`` the generic serde engine handles it automatically —
    ``to_writer`` writes the full stderr stream before the body fields,
    ``from_reader`` reads the stream before the body fields.

    Subclasses define wire-body fields as normal::

        class SomeResponse(WireResponse):
            valid: bool
    """

    logs: WireLogs = PydanticField(default_factory=WireLogs)
