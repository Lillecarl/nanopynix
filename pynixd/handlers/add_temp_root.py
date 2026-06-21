"""Handler for AddTempRoot (op 11)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..serde import AddTempRootRequest, AddTempRootResponse
from ..types.auth import Role
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..types import RequestContext


class AddTempRootHandler(Handler):
    """Server handler for AddTempRoot — admin forwards to daemon, non-admin no-op."""

    op: ClassVar[int] = 11

    async def handle(self, ctx: RequestContext) -> object | None:
        if ctx.role == Role.ADMIN:
            req = await AddTempRootRequest.from_reader(
                ReadContext(reader=ctx.proxy.r, version=ctx.proxy.version),
            )
            return await ctx.proxy.local_store.call(req)
        # Non-admin: consume request body, return no-op success
        await ctx.proxy.r.read_bytes()
        return AddTempRootResponse(value=1)  # type: ignore[return-value]
