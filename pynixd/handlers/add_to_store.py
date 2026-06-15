"""Handler for AddToStore (op 7)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import structlog

from ..operations.add_to_store import AddToStoreRequest, AddToStoreResponse
from ..operations.sign_path_info import SignPathInfoRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext

logger = structlog.get_logger(__name__)


class AddToStoreHandler(Handler):
    """Server handler for AddToStore — streaming with NAR forwarding."""

    op: ClassVar[int] = 7

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        logger.debug("received_op")
        async with ctx.proxy.local_store.transfer_conn() as conn:
            req = object.__new__(AddToStoreRequest)
            await req.forward(ctx.proxy.r, conn.w)
            await conn.w.drain()

            resp = await AddToStoreResponse.deserialize(ReadContext.from_conn(conn))
            if resp.info is not None:
                resp.info = (
                    await ctx.proxy.local_store.execute(
                        SignPathInfoRequest(info=resp.info),
                    )
                ).info
                if resp.info is not None:
                    ctx.proxy.local_store.tracker.add_known_path(resp.info.path)
                    ctx.proxy.local_store.add_path_info(resp.info)
            return resp
