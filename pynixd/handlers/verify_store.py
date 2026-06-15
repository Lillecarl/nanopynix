"""Handler for VerifyStore (op 35)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.base import Role
from ..operations.verify_store import VerifyStoreRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class VerifyStoreHandler(Handler):
    """Server handler for VerifyStore — admin-only."""

    op: ClassVar[int] = 35

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self_req = await VerifyStoreRequest.deserialize(ReadContext.from_request(ctx))

        if ctx.role < Role.ADMIN:
            await ctx.proxy.send_error(
                f"Operation '{self_req.name}' requires administrative privileges.",
            )
            return None

        return await ctx.proxy.execute(self_req)
