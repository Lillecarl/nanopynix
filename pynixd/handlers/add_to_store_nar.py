"""Handler for AddToStoreNar (op 39)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import structlog

from ..operations.add_to_store_nar import AddToStoreNarRequest, AddToStoreNarResponse
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext

logger = structlog.get_logger(__name__)


class AddToStoreNarHandler(Handler):
    """Server handler for AddToStoreNar — streaming with NAR forwarding."""

    op: ClassVar[int] = 39

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        structlog.contextvars.bind_contextvars(operation=type(self).__name__)
        async with ctx.proxy.local_store.transfer_conn() as conn:
            req = object.__new__(AddToStoreNarRequest)
            path = await req.forward(ctx.proxy.r, conn.w)
            resp = await AddToStoreNarResponse.deserialize(ReadContext.from_conn(conn))
            ctx.proxy.local_store.tracker.add_known_path(path)
        return resp
