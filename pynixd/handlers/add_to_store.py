"""Handler for AddToStore (op 7)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import structlog

from ..serde import AddToStoreRequest
from ..serde.add_to_store import AddToStoreResponse as SerdeAddToStoreResponse
from ..serde.sign_path_info import SignPathInfoRequest as SerdeSignPathInfoRequest
from ..store_path import StorePath as OldStorePath
from ..types.context import ReadContext, WriteContext
from ..types.path_info import ValidPathInfo as OldValidPathInfo
from ..wire import forward_framed
from ._base import Handler

if TYPE_CHECKING:
    from ..serde.valid_path_info import ValidPathInfo as SerdeValidPathInfo
    from ..types import RequestContext

logger = structlog.get_logger(__name__)


def _serde_to_old_path_info(info: SerdeValidPathInfo) -> OldValidPathInfo:
    """Convert serde ValidPathInfo to legacy ValidPathInfo for cache."""
    return OldValidPathInfo(
        path=OldStorePath(str(info.path)),
        deriver=OldStorePath(str(info.info.deriver)) if info.info.deriver else OldStorePath(""),
        nar_hash=str(info.info.nar_hash),
        references={OldStorePath(str(r)) for r in info.info.references},
        registration_time=info.info.registration_time.ts,
        nar_size=info.info.nar_size,
        ultimate=1 if info.info.ultimate else 0,
        sigs={sig.to_str() for sig in info.info.sigs},
        ca=str(info.info.ca),
    )


class AddToStoreHandler(Handler):
    """Server handler for AddToStore — streaming with NAR forwarding."""

    op: ClassVar[int] = 7

    async def handle(self, ctx: RequestContext) -> SerdeAddToStoreResponse | None:
        logger.debug("received_op")
        async with ctx.proxy.local_store.transfer_conn() as conn:
            # 1. Read request header from client (serde)
            req = await AddToStoreRequest.from_reader(
                ReadContext(reader=ctx.proxy.r, version=ctx.proxy.version),
            )

            # 2. Write request header to daemon
            await req.to_writer(WriteContext.from_conn(conn))
            await conn.w.drain()

            # 3. Forward framed NAR bytes from client to daemon
            await forward_framed(ctx.proxy.r, conn.w)

            # 4. Read response from daemon
            resp = await SerdeAddToStoreResponse.from_reader(
                ReadContext.from_conn(conn),
            )

        # 5. Sign path info and update cache (outside conn so we don't re-enter the pool)
        if resp.info is not None:
            sign_req = SerdeSignPathInfoRequest(info=resp.info)
            sign_resp = await ctx.proxy.local_store.execute(sign_req)
            resp.info = sign_resp.info

            ctx.proxy.local_store.add_path_info(_serde_to_old_path_info(resp.info))

        return resp
