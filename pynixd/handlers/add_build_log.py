"""Handler for AddBuildLog (op 45)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..operations.add_build_log import AddBuildLogRequest
from ..operations.base import Role
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext


class AddBuildLogHandler(Handler):
    """Server handler for AddBuildLog — admin-only."""

    op: ClassVar[int] = 45

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self_req = await AddBuildLogRequest.deserialize(ReadContext.from_request(ctx))

        if ctx.role < Role.ADMIN:
            await ctx.proxy.send_error(
                f"Operation '{self_req.name}' requires administrative privileges.",
            )
            return None

        return await ctx.proxy.execute(self_req)
