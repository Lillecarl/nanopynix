"""Handler for SignPathInfo (op 107)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.base import Role
from ..operations.sign_path_info import SignPathInfoRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class SignPathInfoHandler(Handler):
    """Server handler for SignPathInfo — admin-only."""

    op: ClassVar[int] = 107

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self_req = await SignPathInfoRequest.deserialize(ReadContext.from_request(ctx))

        if ctx.role < Role.ADMIN:
            await ctx.proxy.send_error(
                f"Operation '{self_req.name}' requires administrative privileges.",
            )
            return None

        return await ctx.proxy.execute(self_req)
