"""
Connection pooling and concurrency limiting for Nix daemon connections.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from ..connection import Connection

log = structlog.get_logger(__name__)


# Per-store connection holder tracking via ContextVar
# Tracks nested connection count per (store_id, kind) tuple
_nested_conns: ContextVar[dict[tuple[str, str], int]] = ContextVar("_nested_conns")


class ConnectionPool:
    """Manages a pool of connections with concurrency limiting and idle TTL.

    Separates "build" and "transfer" slots using two semaphores but
    shares the underlying idle connection pool.
    """

    def __init__(
        self,
        store_id: str,
        factory: Callable[[], Awaitable[Connection]],
        max_builds: int = 2,
        max_transfers: int = 16,
        idle_ttl: float = 10.0,
        on_connection_created: Callable[[Connection], None] | None = None,
    ) -> None:
        self.store_id = store_id
        self.factory = factory
        self.max_builds = max_builds
        self.max_transfers = max_transfers
        self.idle_ttl = idle_ttl
        self.on_connection_created = on_connection_created

        self.build_semaphore = asyncio.Semaphore(max_builds)
        self.transfer_semaphore = asyncio.Semaphore(max_transfers)

        self.idle_conns: list[tuple[Connection, float]] = []
        self.all_conns: list[Connection] = []
        self.sweep_task: asyncio.Task[None] | None = None

    @property
    def in_flight_builds(self) -> int:
        return self.max_builds - self.build_semaphore._value

    @property
    def in_flight_transfers(self) -> int:
        return self.max_transfers - self.transfer_semaphore._value

    @property
    def stats(self) -> str:
        """Human-readable pool statistics."""
        return (
            f"builds={self.in_flight_builds}/{self.max_builds} "
            f"transfers={self.in_flight_transfers}/{self.max_transfers} "
            f"idle={len(self.idle_conns)} total={len(self.all_conns)}"
        )

    async def warm(self, n: int) -> None:
        """Pre-create n connections and park them in the idle pool."""
        conns = await asyncio.gather(*[self.factory() for _ in range(n)])
        now = time.monotonic()
        for conn in conns:
            self.all_conns.append(conn)
            self.idle_conns.append((conn, now))
            if self.on_connection_created:
                self.on_connection_created(conn)
        self.start_sweep()
        log.info("pool_warmed", store_id=self.store_id, connections=n)

    def start_sweep(self) -> None:
        """Start the idle sweep task if not already running."""
        if self.sweep_task is None or self.sweep_task.done():
            self.sweep_task = asyncio.create_task(self.sweep_idle())

    async def sweep_idle(self) -> None:
        """Periodically close idle connections that have expired."""
        while self.idle_conns:
            await asyncio.sleep(self.idle_ttl / 2)
            now = time.monotonic()
            still_idle: list[tuple[Connection, float]] = []
            for conn, returned_at in self.idle_conns:
                if now - returned_at >= self.idle_ttl:
                    log.debug(
                        "pool_closing_expired_idle",
                        store_id=self.store_id,
                        conn_id=conn.id,
                    )
                    if conn in self.all_conns:
                        self.all_conns.remove(conn)
                    try:
                        await conn.close()
                    except Exception:
                        pass
                else:
                    still_idle.append((conn, returned_at))
            self.idle_conns = still_idle

    async def get_or_create_conn(self) -> Connection:
        """Pop an idle connection or create a new one."""
        now = time.monotonic()
        while self.idle_conns:
            candidate, returned_at = self.idle_conns.pop()
            if now - returned_at >= self.idle_ttl:
                log.debug(
                    "pool_discarding_expired",
                    store_id=self.store_id,
                    conn_id=candidate.id,
                )
                if candidate in self.all_conns:
                    self.all_conns.remove(candidate)
                try:
                    await candidate.close()
                except Exception:
                    pass
                continue

            if candidate.dirty or await candidate.r.is_dirty():
                log.warning(
                    "pool_discarding_dirty_conn",
                    store_id=self.store_id,
                    conn_id=candidate.id,
                    op_log=" -> ".join(candidate.op_log[-10:]) or "(empty)",
                )
                if candidate in self.all_conns:
                    self.all_conns.remove(candidate)
                try:
                    await candidate.close()
                except Exception:
                    pass
                continue

            log.debug(
                "pool_reusing_conn",
                store_id=self.store_id,
                conn_id=candidate.id,
            )
            return candidate

        conn = await self.factory()
        self.all_conns.append(conn)
        if self.on_connection_created:
            self.on_connection_created(conn)

        log.debug(
            "pool_created_connection",
            store_id=self.store_id,
            conn_id=conn.id,
            pool_stats=self.stats,
        )
        return conn

    @asynccontextmanager
    async def acquire(
        self,
        kind: str,
    ) -> AsyncIterator[Connection]:
        """Acquire a connection from the shared pool.

        If the same task that is already holding a connection tries to
        acquire again (re-entry), a new connection is allocated outside
        the semaphore to avoid deadlock. A warning is logged for investigation.
        """
        semaphore = (
            self.build_semaphore if kind == "build" else self.transfer_semaphore
        )
        key = (self.store_id, kind)
        
        # Use a copy to avoid mutating a shared default or parent context dict
        counts = _nested_conns.get({}).copy()
        in_use = counts.get(key, 0)
        re_entrant = in_use > 0 and semaphore.locked()

        if re_entrant:
            log.warning(
                "store_reentrant_acquire",
                store_id=self.store_id,
                kind=kind,
                nesting_level=in_use,
            )
            conn = await self.factory()
            self.all_conns.append(conn)
            
            # Increment nesting count in a NEW dict to ensure task isolation
            counts[key] = in_use + 1
            _nested_conns.set(counts)
            
            try:
                async with conn:
                    yield conn
            finally:
                # Decrement nesting count
                counts = _nested_conns.get({}).copy()
                counts[key] = counts.get(key, 0) - 1
                _nested_conns.set(counts)
                
                # Discard re-entrant connections immediately to avoid pool bloat
                if conn in self.all_conns:
                    self.all_conns.remove(conn)
                try:
                    await conn.close()
                except Exception:
                    pass
            return

        # Quiet acquisition — we rely on metrics for global slot monitoring
        await semaphore.acquire()
        conn: Connection | None = None
        try:
            conn = await self.get_or_create_conn()
            
            # Increment nesting count
            counts = _nested_conns.get({}).copy()
            counts[key] = counts.get(key, 0) + 1
            _nested_conns.set(counts)
            
            try:
                async with conn:
                    yield conn
            finally:
                # Decrement nesting count
                counts = _nested_conns.get({}).copy()
                counts[key] = counts.get(key, 0) - 1
                _nested_conns.set(counts)

                if conn is not None:
                    if conn.dirty:
                        log.warning(
                            "store_discarding_dirty_connection",
                            store_id=self.store_id,
                            conn_id=conn.id,
                            op_log=" -> ".join(conn.op_log[-10:]) or "(empty)",
                        )
                        if conn in self.all_conns:
                            self.all_conns.remove(conn)
                        try:
                            await conn.close()
                        except Exception:
                            pass
                    else:
                        self.idle_conns.append((conn, time.monotonic()))
                        self.start_sweep()
        finally:
            semaphore.release()

    def build_conn(self) -> AbstractAsyncContextManager[Connection]:
        return self.acquire("build")

    def transfer_conn(self) -> AbstractAsyncContextManager[Connection]:
        return self.acquire("transfer")

    async def close(self) -> None:
        """Close all pooled connections and stop sweep task."""
        if self.sweep_task is not None:
            self.sweep_task.cancel()
            try:
                await self.sweep_task
            except asyncio.CancelledError:
                pass
            self.sweep_task = None

        for conn in self.all_conns:
            try:
                await conn.close()
            except (ProcessLookupError, Exception):
                pass
        self.all_conns.clear()
        self.idle_conns.clear()
