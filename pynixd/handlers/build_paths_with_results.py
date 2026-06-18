"""Handler for BuildPathsWithResults (op 46)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import structlog

from ..operations.build_paths import (
    BuildPathsWithResultsRequest,
    BuildPathsWithResultsResponse,
)
from ..types.context import ReadContext
from ._base import Handler

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..types import RequestContext

logger = structlog.get_logger(__name__)


class BuildPathsWithResultsHandler(Handler):
    """Server handler for BuildPathsWithResults — scheduler or fallback to daemon."""

    op: ClassVar[int] = 46

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        logger.debug("received_op")

        self_req = await BuildPathsWithResultsRequest.deserialize(ReadContext.from_request(ctx))

        if not ctx.proxy.use_scheduler_for_builds or ctx.proxy.substitution_manager is None:
            logger.debug("handle_local_mode_fallback")
            result = await ctx.proxy.local_store.execute(self_req, client=ctx.proxy.client)

            logger.debug("responded_op")
            return result

        logger.debug(
            "build_paths_with_results_goals",
            num_derivations=len(self_req.derived_paths),
        )
        keyed_results = await ctx.proxy.goal_manager.build_paths(
            self_req.derived_paths,
            ctx.proxy.local_store,
            ctx.proxy.substitution_manager,
            scheduler=ctx.proxy.scheduler,
        )

        for kr in keyed_results:
            if kr.result.status.is_failure:
                logger.warning(
                    "build_paths_with_results_goal_failed",
                    path=kr.path,
                    status=kr.result.status,
                    error=kr.result.error_msg,
                )

        logger.debug("responded_op")
        return BuildPathsWithResultsResponse(results=keyed_results)
