"""Handler for QueryPathInfo (op 26)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.query_path_info import QueryPathInfoRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class QueryPathInfoHandler(Handler):
    """Server handler for QueryPathInfo — delegates to proxy.execute."""

    op: ClassVar[int] = 26

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self = await QueryPathInfoRequest.deserialize(ReadContext.from_request(ctx))
        return await ctx.proxy.execute(self)
