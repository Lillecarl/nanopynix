"""Scheduler-owned substitution queue and availability cache."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import anyio
import structlog
from cachetools import TTLCache

from .exceptions import OpNotImplementedError
from .serde import QueryPathInfoRequest
from .serde import StorePath as SerdeStorePath
from .serde.valid_path_info import ValidPathInfo

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .config import PynixdSettings
    from .context import PynixdContext
    from .serde.ids import StoreId
    from .store import Store
    from .store_path import StorePath

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SubstitutionQueryResult:
    """Cached path-info query result for one path on one substituter."""

    store_id: StoreId
    path_info: ValidPathInfo | None
    query_succeeded: bool

    @property
    def found(self) -> bool:
        return self.path_info is not None


@dataclass(frozen=True)
class SubstitutionAvailability:
    """Fast availability answer for planning and graph traversal."""

    available: bool
    nar_size: int | None = None
    download_size: int | None = None

    @classmethod
    def unavailable(cls) -> SubstitutionAvailability:
        return cls(available=False)

    @classmethod
    def from_query_result(cls, result: SubstitutionQueryResult) -> SubstitutionAvailability:
        if result.path_info is None:
            return cls.unavailable()
        nar_size = result.path_info.info.nar_size
        return cls(available=True, nar_size=nar_size, download_size=nar_size)


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
        self._probe_tasks: set[asyncio.Task[None]] = set()

    async def close(self) -> None:
        for task in self._probe_tasks:
            task.cancel()
        for task in list(self._probe_tasks):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task

    async def can_substitute(self, path: StorePath) -> SubstitutionAvailability:
        cached = self._cached_positive(path)
        if cached is not None:
            return SubstitutionAvailability.from_query_result(cached)

        stores = list(self.substituter_stores())
        if not stores:
            return SubstitutionAvailability.unavailable()

        to_probe = [store for store in stores if not self._has_cached_result(path, store.store_id)]
        if not to_probe:
            return SubstitutionAvailability.unavailable()

        done = asyncio.Event()
        lock = asyncio.Lock()
        first_positive: SubstitutionQueryResult | None = None
        healthy_pending = {store.store_id for store in to_probe if self.should_wait_for(store.store_id)}

        async def probe_store(store: Store) -> None:
            nonlocal first_positive
            result = await self._query_store(path, store)
            self.record_query_result(path, result)
            async with lock:
                if result.found and first_positive is None:
                    first_positive = result
                    done.set()
                healthy_pending.discard(result.store_id)
                if not healthy_pending:
                    done.set()

        for store in to_probe:
            self._track_probe_task(asyncio.create_task(probe_store(store)))

        if not healthy_pending:
            return SubstitutionAvailability.unavailable()

        await done.wait()
        if first_positive is not None:
            return SubstitutionAvailability.from_query_result(first_positive)
        return SubstitutionAvailability.unavailable()

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

    def substituter_stores(self) -> Iterable[Store]:
        return (
            store
            for store_id, store in sorted(self.ctx.stores.items(), key=lambda item: str(item[0]))
            if str(store_id) != "local" and store.no_schedule
        )

    async def _query_store(self, path: StorePath, store: Store) -> SubstitutionQueryResult:
        try:
            with anyio.fail_after(self.ctx.settings.substitution_query_timeout):
                response = await store.execute(QueryPathInfoRequest(path=SerdeStorePath(path=str(path))))
        except OpNotImplementedError:
            return SubstitutionQueryResult(store_id=store.store_id, path_info=None, query_succeeded=False)
        except TimeoutError:
            log.warning("substitution_query_timeout", store_id=store.store_id, path=str(path))
            return SubstitutionQueryResult(store_id=store.store_id, path_info=None, query_succeeded=False)
        except Exception:
            log.warning("substitution_query_failed", store_id=store.store_id, path=str(path), exc_info=True)
            return SubstitutionQueryResult(store_id=store.store_id, path_info=None, query_succeeded=False)

        if not response.valid or response.info is None:
            return SubstitutionQueryResult(store_id=store.store_id, path_info=None, query_succeeded=True)

        path_info = ValidPathInfo(path=SerdeStorePath(path=str(path)), info=response.info)
        return SubstitutionQueryResult(store_id=store.store_id, path_info=path_info, query_succeeded=True)

    def _cached_positive(self, path: StorePath) -> SubstitutionQueryResult | None:
        for result in self.positive.get(path, {}).values():
            if result.found:
                return result
        return None

    def _has_cached_result(self, path: StorePath, store_id: StoreId) -> bool:
        return store_id in self.positive.get(path, {}) or store_id in self.negative.get(path, {})

    def _track_probe_task(self, task: asyncio.Task[None]) -> None:
        self._probe_tasks.add(task)

        def done_callback(done_task: asyncio.Task[None]) -> None:
            self._probe_tasks.discard(done_task)
            if done_task.cancelled():
                return
            try:
                done_task.result()
            except Exception:
                log.warning("substitution_probe_task_failed", exc_info=True)

        task.add_done_callback(done_callback)
