"""Handler for QueryMissing (op 40)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import structlog

from ..operations.query_missing import QueryMissingRequest
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext

logger = structlog.get_logger(__name__)


class QueryMissingHandler(Handler):
    """Server handler for QueryMissing — goal manager or fallback to daemon."""

    op: ClassVar[int] = 40

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        logger.debug("received_op")
        self_req = await QueryMissingRequest.deserialize(ReadContext.from_request(ctx))

        if ctx.proxy.substitution_manager is None:
            return await self_req.execute(
                ctx.proxy.local_store,
                client=ctx.proxy.client,
            )

        logger.debug("query_missing_goals", count=len(self_req.derived_paths))
        return await ctx.proxy.goal_manager.query_paths(
            self_req.derived_paths,
            ctx.proxy.local_store,
            ctx.proxy.substitution_manager,
            scheduler=ctx.proxy.scheduler,
        )
