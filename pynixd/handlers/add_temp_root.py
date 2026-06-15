"""Handler for AddTempRoot (op 11)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.add_temp_root import AddTempRootRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class AddTempRootHandler(Handler):
    """Server handler for AddTempRoot — delegates to proxy.execute."""

    op: ClassVar[int] = 11

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self = await AddTempRootRequest.deserialize(ReadContext.from_request(ctx))
        return await ctx.proxy.execute(self)
