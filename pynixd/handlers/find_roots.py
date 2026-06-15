"""Handler for FindRoots (op 14)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.find_roots import FindRootsRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class FindRootsHandler(Handler):
    """Server handler for FindRoots — delegates to proxy.execute."""

    op: ClassVar[int] = 14

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self = await FindRootsRequest.deserialize(ReadContext.from_request(ctx))
        return await ctx.proxy.execute(self)
