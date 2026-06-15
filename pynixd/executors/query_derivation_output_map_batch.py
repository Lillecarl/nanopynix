"""Executor for QueryDerivationOutputMapBatch (op 106) — SQLite fast-path, falls through to daemon."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from pynixd.operations.query_derivation_output_map_batch import (
    QUERY_DERIVATION_OUTPUT_MAP_BATCH,
    DerivationOutputMapBatchResponse,
)
from pynixd.store_path import StorePath

from ._base import Executor

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..store.base import Store


class QueryDerivationOutputMapBatchExecutor(Executor):
    """Fast-path for QueryDerivationOutputMapBatch — SQLite DerivationOutputs table, falls through to daemon."""

    op: ClassVar[int] = 106

    async def execute(
        self,
        request: Any,
        store: Store,
        client: Any = None,
        suppress_last: bool = False,
    ) -> OpResponse | None:
        if not request.drv_paths:
            return DerivationOutputMapBatchResponse({})

        if (db := store.db) is not None:
            paths_json = json.dumps([str(p) for p in request.drv_paths])
            async with db.execute(
                QUERY_DERIVATION_OUTPUT_MAP_BATCH,
                (paths_json,),
            ) as cursor:
                rows = await cursor.fetchall()

            result: dict = {}
            for drv_path, output_name, output_path in rows:
                result.setdefault(StorePath(drv_path), {})[output_name] = (
                    StorePath(output_path) if output_path else None
                )

            for drv_path in request.drv_paths:
                if StorePath(drv_path) in result:
                    continue
                try:
                    parsed = await store.read_derivation(drv_path)
                    if parsed is None:
                        continue
                    result[StorePath(drv_path)] = dict(parsed.output_paths().items())
                except FileNotFoundError:
                    pass

            return DerivationOutputMapBatchResponse(outputs=result)

        return None  # fall through to daemon
