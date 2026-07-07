"""Scheduler-owned substitution queue and availability cache."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from cachetools import TTLCache

if TYPE_CHECKING:
    from .config import PynixdSettings
    from .context import PynixdContext
    from .serde.ids import StoreId
    from .serde.valid_path_info import ValidPathInfo
    from .store_path import StorePath


@dataclass(frozen=True)
class SubstitutionQueryResult:
    """Cached path-info query result for one path on one substituter."""

    store_id: StoreId
    path_info: ValidPathInfo | None
    query_succeeded: bool

    @property
    def found(self) -> bool:
        return self.path_info is not None


class SubstitutionHealthLog:
    """Fixed-size per-store substitution query health log."""

    def __init__(self, settings: PynixdSettings) -> None:
        self._entries: deque[bool] = deque(maxlen=settings.substitution_health_window)
        self._min_entries = max(
            1,
            int(settings.substitution_health_window * settings.substitution_health_min_fill_ratio),
        )
        self._min_success_ratio = settings.substitution_health_min_success_ratio

    def record(self, *, query_succeeded: bool) -> None:
        self._entries.append(query_succeeded)

    @property
    def is_healthy(self) -> bool:
        if len(self._entries) < self._min_entries:
            return True
        successes = sum(1 for entry in self._entries if entry)
        return successes / len(self._entries) > self._min_success_ratio


class SubstitutionQueue:
    """Owns substitution availability state for the scheduler singleton."""

    def __init__(self, ctx: PynixdContext) -> None:
        self.ctx = ctx
        settings = ctx.settings
        self.positive = cast(
            "TTLCache[StorePath, dict[StoreId, SubstitutionQueryResult], float]",
            TTLCache(maxsize=settings.substitution_cache_maxsize, ttl=settings.substitution_positive_ttl),
        )
        self.negative = cast(
            "TTLCache[StorePath, dict[StoreId, SubstitutionQueryResult], float]",
            TTLCache(maxsize=settings.substitution_cache_maxsize, ttl=settings.substitution_negative_ttl),
        )
        self.health: dict[StoreId, SubstitutionHealthLog] = {}

    def record_query_result(self, path: StorePath, result: SubstitutionQueryResult) -> None:
        cache = self.positive if result.found else self.negative
        per_store = cache.setdefault(path, {})
        per_store[result.store_id] = result
        self.health_for(result.store_id).record(query_succeeded=result.query_succeeded)

    def health_for(self, store_id: StoreId) -> SubstitutionHealthLog:
        log = self.health.get(store_id)
        if log is None:
            log = SubstitutionHealthLog(self.ctx.settings)
            self.health[store_id] = log
        return log

    def should_wait_for(self, store_id: StoreId) -> bool:
        return self.health_for(store_id).is_healthy
