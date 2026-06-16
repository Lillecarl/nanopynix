"""Handler for VerifyStore (op 35)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.base import Role
from ..serde import VerifyStoreRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class VerifyStoreHandler(Handler):
    """Server handler for VerifyStore — admin-only."""

    op: ClassVar[int] = 35

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        req = await VerifyStoreRequest.from_reader(
            ReadContext(reader=ctx.proxy.r, version=ctx.proxy.version),
        )

        if ctx.role < Role.ADMIN:
            await ctx.proxy.send_error(
                "Operation 'VerifyStore' requires administrative privileges.",
            )
            return None

        return await ctx.proxy.local_store.call(req)
