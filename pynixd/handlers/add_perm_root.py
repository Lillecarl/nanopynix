"""Handler for AddPermRoot (op 47)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.add_perm_root import AddPermRootResponse as OldAddPermRootResponse
from ..operations.base import Role
from ..serde import AddPermRootRequest
from ..stderr import StderrNext
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class AddPermRootHandler(Handler):
    """Server handler for AddPermRoot — no-op for non-admin, forwards to daemon for admin."""

    op: ClassVar[int] = 47

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        if ctx.role == Role.ADMIN:
            req = await AddPermRootRequest.from_reader(
                ReadContext(reader=ctx.proxy.r, version=ctx.proxy.version),
            )
            return await ctx.proxy.local_store.call(req)

        # Non-admin: consume request body, return no-op success
        await ctx.proxy.r.read_bytes()
        await ctx.proxy.r.read_bytes()
        resp = OldAddPermRootResponse(gc_root="")
        msg = StderrNext("pynixd: AddPermRoot ignored (no-op)")
        resp.logs.add(msg)
        return resp
