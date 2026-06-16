"""Handler for AddTempRoot (op 11)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..serde import AddTempRootRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class AddTempRootHandler(Handler):
    """Server handler for AddTempRoot — delegates to local_store.call()."""

    op: ClassVar[int] = 11

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        req = await AddTempRootRequest.from_reader(
            ReadContext(reader=ctx.proxy.r, version=ctx.proxy.version),
        )
        return await ctx.proxy.local_store.call(req)
