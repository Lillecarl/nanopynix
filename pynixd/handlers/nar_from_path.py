"""Handler for NarFromPath (op 38)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import structlog

from .. import wire
from ..operations.base import OperationLogs
from ..operations.nar_from_path import NarFromPathRequest, NarFromPathResponse
from ..operations.query_path_info import QueryPathInfoRequest
from ..store_path import StorePath
from ..types.context import ReadContext, WriteContext
from ..wire import _CHUNK_SIZE
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext

logger = structlog.get_logger(__name__)


class NarFromPathHandler(Handler):
    """Server handler for NarFromPath — streaming NAR data to client."""

    op: ClassVar[int] = 38

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        path = await ctx.proxy.r.read_string(StorePath)

        info_resp = await ctx.proxy.local_store.execute(QueryPathInfoRequest(path=path))
        if not info_resp.valid or info_resp.info is None:
            logger.warning("nar_not_in_local_store", path=path)
            logger.debug("responded_op")
            return NarFromPathResponse(nar_data=b"")

        nar_size = info_resp.info.nar_size

        logger.debug("nar_from_path_streaming", path=path, size=nar_size)

        async with ctx.proxy.local_store.transfer_conn() as conn:
            await NarFromPathRequest(
                path=path,
                nar_size=nar_size,
            ).serialize(WriteContext.from_conn(conn))
            await conn.w.drain()

            logs = await OperationLogs.deserialize(ReadContext.from_conn(conn))

            await ctx.proxy.client.flush()
            logs.serialize(WriteContext(writer=ctx.proxy.w, version=ctx.version))

            if nar_size > 0:
                remaining = nar_size
                while remaining > 0:
                    to_read = min(remaining, _CHUNK_SIZE)
                    chunk = await conn.r.readexactly(to_read)
                    ctx.proxy.w.write(chunk)
                    remaining -= to_read
            else:
                await wire.stream_parse_nar(conn.r, ctx.proxy.w)

        await ctx.proxy.w.drain()
        logger.debug("responded_op")
        return None
