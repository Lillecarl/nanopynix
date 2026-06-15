"""Handler for OptimiseStore (op 34)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.base import Role
from ..operations.optimise_store import OptimiseStoreRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class OptimiseStoreHandler(Handler):
    """Server handler for OptimiseStore — admin-only."""

    op: ClassVar[int] = 34

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self_req = await OptimiseStoreRequest.deserialize(ReadContext.from_request(ctx))

        if ctx.role < Role.ADMIN:
            await ctx.proxy.send_error(
                f"Operation '{self_req.name}' requires administrative privileges.",
            )
            return None

        return await ctx.proxy.execute(self_req)
