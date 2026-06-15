"""Handler for QueryValidDerivers (op 33)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.query_valid_derivers import QueryValidDeriversRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class QueryValidDeriversHandler(Handler):
    """Server handler for QueryValidDerivers — delegates to proxy.execute."""

    op: ClassVar[int] = 33

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self = await QueryValidDeriversRequest.deserialize(ReadContext.from_request(ctx))
        return await ctx.proxy.execute(self)
