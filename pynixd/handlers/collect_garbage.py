"""Handler for CollectGarbage (op 20)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..serde import CollectGarbageRequest
from ..types.auth import Role
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..types import RequestContext


class CollectGarbageHandler(Handler):
    """Server handler for CollectGarbage — admin-only."""

    op: ClassVar[int] = 20

    async def handle(self, ctx: RequestContext) -> object | None:
        req = await CollectGarbageRequest.from_reader(
            ReadContext(reader=ctx.proxy.r, version=ctx.proxy.version),
        )

        if ctx.role < Role.ADMIN:
            await ctx.proxy.send_error(
                "Operation 'CollectGarbage' requires administrative privileges.",
            )
            return None

        return await ctx.proxy.local_store.call(req)
