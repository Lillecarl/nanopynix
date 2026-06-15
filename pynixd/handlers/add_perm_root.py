"""Handler for AddPermRoot (op 47)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.add_perm_root import AddPermRootRequest, AddPermRootResponse
from ..operations.base import Role
from ..stderr import StderrNext
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class AddPermRootHandler(Handler):
    """Server handler for AddPermRoot — no-op for non-admin, delegates to proxy.execute for admin."""

    op: ClassVar[int] = 47

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self_req = await AddPermRootRequest.deserialize(ReadContext.from_request(ctx))

        if ctx.role < Role.ADMIN:
            resp = AddPermRootResponse(gc_root="")
            msg = StderrNext("pynixd: AddPermRoot ignored (no-op)")
            resp.logs.add(msg)
            return resp

        return await ctx.proxy.execute(self_req)
