"""Executor for QueryDerivationOutputMap (op 41) — no SQLite fast-path, always falls through to daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from ._base import Executor

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..store.base import Store


class QueryDerivationOutputMapExecutor(Executor):
    """No local fast-path for QueryDerivationOutputMap — falls through to daemon."""

    op: ClassVar[int] = 41

    async def execute(
        self,
        request: Any,
        store: Store,
        client: Any = None,
        suppress_last: bool = False,
    ) -> OpResponse | None:
        return None  # fall through to daemon
