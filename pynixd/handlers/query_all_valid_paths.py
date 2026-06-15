"""Handler for QueryAllValidPaths (op 23)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.query_all_valid_paths import QueryAllValidPathsRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class QueryAllValidPathsHandler(Handler):
    """Server handler for QueryAllValidPaths — delegates to proxy.execute."""

    op: ClassVar[int] = 23

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self = await QueryAllValidPathsRequest.deserialize(ReadContext.from_request(ctx))
        return await ctx.proxy.execute(self)
