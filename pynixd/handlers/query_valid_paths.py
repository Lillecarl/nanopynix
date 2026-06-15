"""Handler for QueryValidPaths (op 31)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.query_valid_paths import QueryValidPathsRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class QueryValidPathsHandler(Handler):
    """Server handler for QueryValidPaths — delegates to proxy.execute."""

    op: ClassVar[int] = 31

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self = await QueryValidPathsRequest.deserialize(ReadContext.from_request(ctx))
        return await ctx.proxy.execute(self)
