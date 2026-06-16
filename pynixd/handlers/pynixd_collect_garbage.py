"""Handler for PynixdCollectGarbage (op 101)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.base import Role
from ..serde import PynixdCollectGarbageRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class PynixdCollectGarbageHandler(Handler):
    """Server handler for PynixdCollectGarbage — admin-only."""

    op: ClassVar[int] = 101

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        req = await PynixdCollectGarbageRequest.from_reader(
            ReadContext(reader=ctx.proxy.r, version=ctx.proxy.version),
        )

        if ctx.role < Role.ADMIN:
            await ctx.proxy.send_error(
                "Operation 'PynixdCollectGarbage' requires administrative privileges.",
            )
            return None

        return await ctx.proxy.local_store.call(req)
