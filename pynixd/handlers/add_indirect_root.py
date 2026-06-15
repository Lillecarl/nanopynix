"""Handler for AddIndirectRoot (op 12)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.add_indirect_root import AddIndirectRootRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class AddIndirectRootHandler(Handler):
    """Server handler for AddIndirectRoot — delegates to proxy.execute."""

    op: ClassVar[int] = 12

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self = await AddIndirectRootRequest.deserialize(ReadContext.from_request(ctx))
        return await ctx.proxy.execute(self)
