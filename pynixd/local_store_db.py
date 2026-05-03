"""Direct SQLite access to the local Nix store database.

Provides a connection pool for direct SQL queries against the local store DB.
Operations that support fast-path SQL queries use ``store.db.acquire_conn()``
directly rather than going through a dispatcher.

Registration time updates are batched writes using Lix's recursive CTE
that touches the full closure (references, derivers, deriver references)
of each seed path.

If the database can't be opened (permissions, missing file, wrong schema),
logs a warning and becomes unavailable — callers fall back to the daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import structlog

from .store_path import StorePath

from .types.aliases import StorePathSet
if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .types.ids import StoreId

log = structlog.get_logger(__name__)


# ── SQL constants ─────────────────────────────────────────────────────

QUERY_STALE_PATHS = """
SELECT path FROM ValidPaths
WHERE registrationTime > 0 AND registrationTime < ?
"""

# Lix's UpdateRegistrationTimeRecursive — walks the full closure
UPDATE_REGTIME = """
UPDATE ValidPaths
SET registrationTime = unixepoch()
WHERE id IN (
    WITH RECURSIVE closure(id) AS (
        SELECT id FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))
        UNION
        SELECT r.reference
        FROM closure c JOIN Refs r ON c.id = r.referrer
        UNION
        SELECT deriver_vp.id
        FROM closure c
        JOIN ValidPaths current_vp ON c.id = current_vp.id
        JOIN ValidPaths deriver_vp ON current_vp.deriver = deriver_vp.path
        WHERE current_vp.deriver IS NOT NULL
        UNION
        SELECT r.reference
        FROM closure c
        JOIN ValidPaths current_vp ON c.id = current_vp.id
        JOIN ValidPaths deriver_vp ON current_vp.deriver = deriver_vp.path
        JOIN Refs r ON deriver_vp.id = r.referrer
        WHERE current_vp.deriver IS NOT NULL
    )
    SELECT id FROM closure
);
"""

INSERT_KNOWN_PATHS = """
INSERT OR IGNORE INTO PynixdKnownPaths (storeId, path)
SELECT ?, value FROM json_each(?)
"""

REMOVE_KNOWN_PATHS = """
DELETE FROM PynixdKnownPaths
WHERE storeId = ? AND path IN (SELECT value FROM json_each(?))
"""

GET_KNOWN_PATHS = """
SELECT path FROM PynixdKnownPaths WHERE storeId = ?
"""

DELETE_STORE_KNOWN_PATHS = """
DELETE FROM PynixdKnownPaths WHERE storeId = ?
"""

INSERT_BUILD_STATS = """
INSERT OR REPLACE INTO DerivationStats
(pname, version, platform, serialized_drv, cpu_user_us, cpu_system_us, duration_ms, last_built_at)
VALUES (?, ?, ?, ?, ?, ?, ?, unixepoch())
"""

QUERY_BUILD_STATS_HINT = """
WITH matching_pname AS (
    SELECT * FROM DerivationStats
    WHERE pname = ? AND platform = ?
)
SELECT duration_ms FROM matching_pname
ORDER BY levenshtein(serialized_drv, ?) ASC
LIMIT 1
"""

QUERY_BUILD_STATS_CROSS_PLATFORM = """
SELECT AVG(duration_ms) FROM DerivationStats
WHERE pname = ?
"""

_DEFAULT_REGTIME_FLUSH_INTERVAL = 5.0


def levenshtein_distance(s1: str, s2: str) -> int:
    """Simple Levenshtein distance implementation for SQLite matching."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if not s2:
        return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


class LocalStoreDB:
    """Connection pool and dispatcher for Nix store SQLite.

    Operation types implement their own DB logic via ``execute_db(db)``.
    This class only manages connections and dispatches.

    Use the async factory ``await LocalStoreDB.open(store_path)`` to create.
    """

    def __init__(
        self,
        db_path: Path | None,
        store_path: Path | None,
        read_only: bool,
        regtime_flush_interval: float,
        max_conns: int = 8,
    ) -> None:
        self.db_path = db_path
        self.store_path = store_path
        self.read_only: bool = read_only
        self.regtime_flush_interval = regtime_flush_interval

        self.pending_regtime: StorePathSet = set()
        self.pending_known_paths: dict[StoreId, StorePathSet] = {}
        self.pending_removed_known_paths: dict[StoreId, StorePathSet] = {}
        self.flush_task: asyncio.Task[None] | None = None

        self._all_conns: list[aiosqlite.Connection] = []
        self._idle_conns: list[aiosqlite.Connection] = []
        self._pool_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_conns)

    @property
    def active(self) -> bool:
        return self.db_path is not None

    @asynccontextmanager
    async def acquire_conn(self) -> AsyncIterator[aiosqlite.Connection]:
        """Acquire a connection from the pool."""
        if not self.active:
            raise RuntimeError("Database not active")

        await self._sem.acquire()
        conn: aiosqlite.Connection | None = None
        try:
            async with self._pool_lock:
                if self._idle_conns:
                    conn = self._idle_conns.pop()

            if conn is None:
                mode = "ro" if self.read_only else "rw"
                uri = f"file:{self.db_path}?mode={mode}"
                conn = await aiosqlite.connect(uri, uri=True)
                await conn.create_function("levenshtein", 2, levenshtein_distance)
                async with self._pool_lock:
                    self._all_conns.append(conn)

            yield conn
        finally:
            if conn is not None:
                async with self._pool_lock:
                    self._idle_conns.append(conn)
            self._sem.release()

    @asynccontextmanager
    async def execute(
        self,
        query: str,
        params: tuple = (),
    ) -> AsyncIterator[aiosqlite.Cursor]:
        """Execute a query and return the cursor.

        Convenience method that acquires a connection, runs the query,
        and returns the cursor. Usage::

            async with db.execute("SELECT * FROM Foo WHERE id = ?", (id,)) as cursor:
                row = await cursor.fetchone()

        For multiple queries or complex flows, use acquire_conn() directly.
        """
        async with self.acquire_conn() as conn:
            cursor = await conn.execute(query, params)
            yield cursor

    @classmethod
    async def open(
        cls,
        store_path: Path,
        regtime_flush_interval: float = _DEFAULT_REGTIME_FLUSH_INTERVAL,
    ) -> LocalStoreDB:
        """Open the Nix store database. Returns an instance (possibly with no DB)."""
        db_path = resolve_db_path(store_path)
        if db_path is None:
            return cls(
                db_path=None,
                store_path=store_path,
                read_only=True,
                regtime_flush_interval=regtime_flush_interval,
            )

        db_dir = db_path.parent
        can_write = os.access(db_dir, os.W_OK)
        read_only = not can_write

        try:
            instance = cls(
                db_path=db_path,
                store_path=store_path,
                read_only=read_only,
                regtime_flush_interval=regtime_flush_interval,
            )

            async with instance.acquire_conn() as db:
                if not read_only:
                    await db.execute("PRAGMA journal_mode=WAL")
                    await db.execute(
                        "CREATE TABLE IF NOT EXISTS PynixdKnownPaths ("
                        "storeId TEXT, "
                        "path TEXT, "
                        "PRIMARY KEY (storeId, path)"
                        ")",
                    )
                    await db.execute(
                        "CREATE TABLE IF NOT EXISTS DerivationStats ("
                        "pname TEXT, "
                        "version TEXT, "
                        "platform TEXT, "
                        "serialized_drv TEXT, "
                        "cpu_user_us INTEGER, "
                        "cpu_system_us INTEGER, "
                        "duration_ms INTEGER, "
                        "last_built_at INTEGER, "
                        "PRIMARY KEY (pname, version, platform, serialized_drv)"
                        ")",
                    )
                    await db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_drv_stats_lookup ON DerivationStats(pname, platform)",
                    )
                await db.execute("SELECT 1 FROM ValidPaths LIMIT 1")

        except Exception as e:
            log.warning(
                "nix_db_open_failed",
                db_path=db_path,
                error=e,
            )
            return cls(
                db_path=None,
                store_path=store_path,
                read_only=True,
                regtime_flush_interval=regtime_flush_interval,
            )
        else:
            log.info(
                "local_store_db_active",
                db_path=db_path,
                mode="read-write" if not read_only else "read-only",
            )
            return instance

    # ── Internal utility queries ──────────────────────────────────────
    # These are not operation dispatches but internal helpers used by
    # non-operation code (GC, build planner, http cache).

    async def query_stale_paths(self, max_age_seconds: int) -> StorePathSet | None:
        """Find paths with registrationTime older than max_age_seconds ago."""
        if not self.active:
            return None
        try:
            cutoff = int(time.time()) - max_age_seconds
            async with self.execute(QUERY_STALE_PATHS, (cutoff,)) as cursor:
                rows = await cursor.fetchall()
            return {StorePath(r[0]) for r in rows}
        except Exception:
            log.debug("query_stale_paths_failed", exc_info=True)
            return None

    # ── Registration time updates ─────────────────────────────────────

    def mark_path(self, path: StorePath) -> None:
        """Queue a path for registration time update."""
        if self.active and not self.read_only:
            self.pending_regtime.add(path)

    def mark_paths(self, paths: StorePathSet) -> None:
        """Queue multiple paths for registration time update."""
        if self.active and not self.read_only:
            self.pending_regtime.update(paths)

    def mark_known_paths(self, store_id: StoreId, paths: StorePathSet) -> None:
        """Queue paths to be recorded as known on a specific store."""
        if self.active and not self.read_only:
            if store_id not in self.pending_known_paths:
                self.pending_known_paths[store_id] = set()
            self.pending_known_paths[store_id].update(paths)

    def unmark_known_paths(self, store_id: StoreId, paths: StorePathSet) -> None:
        """Queue paths to be removed from the known paths for a store."""
        if self.active and not self.read_only:
            if store_id not in self.pending_removed_known_paths:
                self.pending_removed_known_paths[store_id] = set()
            self.pending_removed_known_paths[store_id].update(paths)

    async def get_known_paths(
        self,
        store_id: StoreId,
        conn: aiosqlite.Connection | None = None,
    ) -> StorePathSet:
        """Fetch all known paths for a store from the DB."""
        if not self.active:
            return set()
        try:
            if conn:
                async with conn.execute(GET_KNOWN_PATHS, (store_id,)) as cursor:
                    rows = await cursor.fetchall()
            else:
                async with self.execute(GET_KNOWN_PATHS, (store_id,)) as cursor:
                    rows = await cursor.fetchall()
            return {StorePath(r[0]) for r in rows}
        except Exception:
            log.warning("get_known_paths_failed", store_id=store_id, exc_info=True)
            return set()

    async def remove_store_paths(self, store_id: StoreId) -> None:
        """Remove all known path records for a store from the DB."""
        if not self.active or self.read_only:
            return
        try:
            async with self.acquire_conn() as db:
                await db.execute(DELETE_STORE_KNOWN_PATHS, (store_id,))
                await db.commit()
            log.info("removed_store_paths", store_id=store_id)
        except Exception:
            log.warning("remove_store_paths_failed", store_id=store_id, exc_info=True)

    async def record_build_stats(
        self,
        pname: str,
        version: str,
        platform: str,
        serialized_drv: str,
        cpu_user_us: int | None,
        cpu_system_us: int | None,
        duration_ms: int,
    ) -> None:
        """Record build statistics for a derivation."""
        if not self.active or self.read_only:
            return
        try:
            async with self.acquire_conn() as db:
                await db.execute(
                    INSERT_BUILD_STATS,
                    (
                        pname,
                        version,
                        platform,
                        serialized_drv,
                        cpu_user_us,
                        cpu_system_us,
                        duration_ms,
                    ),
                )
                await db.commit()
        except Exception:
            log.warning("record_build_stats_failed", pname=pname, exc_info=True)

    async def get_build_stats_hint(
        self,
        pname: str,
        platform: str,
        serialized_drv: str,
    ) -> int | None:
        """Get an expected duration hint for a derivation (in ms)."""
        if not self.active:
            return None
        try:
            # 1. Try exact match or closest Levenshtein on same platform
            async with self.execute(
                QUERY_BUILD_STATS_HINT,
                (pname, platform, serialized_drv),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return int(row[0])

            # 2. Fallback to platform-agnostic average for this pname
            async with self.execute(
                QUERY_BUILD_STATS_CROSS_PLATFORM,
                (pname,),
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0] is not None:
                    return int(row[0])
        except Exception:
            log.debug("get_build_stats_hint_failed", pname=pname, exc_info=True)
        return None

    async def flush_regtime(self) -> None:
        """Flush pending registration time updates to SQLite."""
        if not self.active or self.read_only:
            return
        if not self.pending_regtime and not self.pending_known_paths and not self.pending_removed_known_paths:
            return

        paths = self.pending_regtime
        self.pending_regtime = set()

        known_paths = self.pending_known_paths
        self.pending_known_paths = {}

        removed_known_paths = self.pending_removed_known_paths
        self.pending_removed_known_paths = {}

        try:
            t0 = time.monotonic()
            async with self.acquire_conn() as db:
                if paths:
                    paths_json = json.dumps(list(paths))
                    await db.execute(UPDATE_REGTIME, (paths_json,))
                for sid, pths in known_paths.items():
                    await db.execute(INSERT_KNOWN_PATHS, (sid, json.dumps(list(pths))))
                for sid, pths in removed_known_paths.items():
                    await db.execute(REMOVE_KNOWN_PATHS, (sid, json.dumps(list(pths))))
                await db.commit()
            elapsed = time.monotonic() - t0
            log.debug(
                "db_flush_complete",
                regtime_count=len(paths),
                known_stores=len(known_paths),
                removed_stores=len(removed_known_paths),
                elapsed_ms=elapsed * 1000,
            )
        except Exception:
            log.exception("db_flush_failed")
            await self.close_db_pool()

    def start(self) -> None:
        """Start background regtime flush task. Call from async context."""
        if not self.active or self.flush_task is not None or self.read_only:
            return
        self.flush_task = asyncio.create_task(self.flush_loop())

    async def flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.regtime_flush_interval)
                try:
                    await self.flush_regtime()
                except Exception:
                    log.exception("db_flush_loop_iteration_failed")
        except asyncio.CancelledError:
            with suppress(Exception):
                await self.flush_regtime()
        except Exception:
            log.exception("db_flush_loop_crashed")

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def close(self) -> None:
        """Stop flush task, flush pending writes, close database."""
        if self.flush_task is not None:
            self.flush_task.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await self.flush_task
            self.flush_task = None
        await self.flush_regtime()
        await self.close_db_pool()

    async def close_db_pool(self) -> None:
        async with self._pool_lock:
            for db in self._all_conns:
                with suppress(Exception):
                    await db.close()
            self._all_conns.clear()
            self._idle_conns.clear()
            self.db_path = None


def resolve_db_path(store_path: Path) -> Path | None:
    """Compute path to db.sqlite for a given store root."""
    if store_path == Path("/") or not store_path:
        db_path = Path("/nix/var/nix/db/db.sqlite")
    else:
        db_path = store_path / "nix" / "var" / "nix" / "db" / "db.sqlite"

    if not db_path.exists():
        log.warning("nix_db_not_found", db_path=db_path)
        return None
    return db_path
