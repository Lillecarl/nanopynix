"""Handler for SetOptions (op 19)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..serde import SetOptionsRequest, SetOptionsResponse
from ..serde.logs import LogNext
from ..types.auth import Role
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..types import RequestContext


class SetOptionsHandler(Handler):
    """Server handler for SetOptions — admin-only, no-op for others."""

    op: ClassVar[int] = 19

    async def handle(self, ctx: RequestContext) -> object | None:
        req = await SetOptionsRequest.from_reader(
            ReadContext(reader=ctx.proxy.r, version=ctx.proxy.version),
        )

        if ctx.role < Role.ADMIN:
            resp = SetOptionsResponse()
            resp.logs.add(LogNext(text="pynixd: SetOptions ignored (no-op)"))
            return resp

        return await ctx.proxy.local_store.call(req)
