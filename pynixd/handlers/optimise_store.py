"""Handler for OptimiseStore (op 34)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.base import Role
from ..serde import OptimiseStoreRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class OptimiseStoreHandler(Handler):
    """Server handler for OptimiseStore — admin-only."""

    op: ClassVar[int] = 34

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        req = await OptimiseStoreRequest.from_reader(
            ReadContext(reader=ctx.proxy.r, version=ctx.proxy.version),
        )

        if ctx.role < Role.ADMIN:
            await ctx.proxy.send_error(
                "Operation 'OptimiseStore' requires administrative privileges.",
            )
            return None

        return await ctx.proxy.local_store.call(req)
