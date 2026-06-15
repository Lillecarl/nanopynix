"""Handler for QueryPathFromHashPart (op 29)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.query_path_from_hash_part import QueryPathFromHashPartRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class QueryPathFromHashPartHandler(Handler):
    """Server handler for QueryPathFromHashPart — delegates to proxy.execute."""

    op: ClassVar[int] = 29

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self = await QueryPathFromHashPartRequest.deserialize(ReadContext.from_request(ctx))
        return await ctx.proxy.execute(self)
