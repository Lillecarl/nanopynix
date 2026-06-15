"""Handler for IsValidPath (op 1)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.is_valid_path import IsValidPathRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class IsValidPathHandler(Handler):
    """Server handler for IsValidPath — delegates to proxy.execute."""

    op: ClassVar[int] = 1

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self = await IsValidPathRequest.deserialize(ReadContext.from_request(ctx))
        return await ctx.proxy.execute(self)
