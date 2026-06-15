"""Handler for AddMultipleToStore (op 44)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar

import structlog

from ..operations.add_multiple_to_store import (
    AddMultipleToStoreRequest,
    AddMultipleToStoreResponse,
)
from ..types.context import ReadContext, WriteContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext

logger = structlog.get_logger(__name__)


class AddMultipleToStoreHandler(Handler):
    """Server handler for AddMultipleToStore — streaming with framed NAR forwarding."""

    op: ClassVar[int] = 44

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        async with ctx.proxy.local_store.transfer_conn() as conn:
            req = object.__new__(AddMultipleToStoreRequest)
            req = await req.deserialize(ReadContext.from_request(ctx))

            await req.serialize(WriteContext.from_conn(conn))
            await conn.w.drain()

            async def _read_response() -> AddMultipleToStoreResponse:
                try:
                    return await AddMultipleToStoreResponse.deserialize(
                        ReadContext.from_conn(conn),
                    )
                except Exception:
                    logger.exception("add_multiple_to_store_response_failed")
                    raise

            async with asyncio.TaskGroup() as tg:
                resp_task = tg.create_task(_read_response())
                infos = await req.forward_stream(ctx.proxy.r, conn.w)
                resp = await resp_task

            ctx.proxy.local_store.add_path_infos(infos)
            ctx.proxy.local_store.tracker.add_known_paths({i.path for i in infos})
            return resp
