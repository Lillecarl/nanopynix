"""Direct SQLite access to the local Nix store database.

Provides a connection pool and dispatcher for operation types that
implement their own DB fast-paths via ``execute_db(db)``.

Registration time updates are batched writes using Lix's recursive CTE
that touches the full closure (references, derivers, deriver references)
of each seed path.

If the database can't be opened (permissions, missing file, wrong schema),
logs a warning and becomes unavailable — callers fall back to the daemon.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import structlog

from .store_path import StorePath

if TYPE_CHECKING:
    from .operations.base import OpRequest, Resp

log = structlog.get_logger(__name__)

# ── SQL constants (imported by operation types) ──────────────────────

IS_VALID_PATH = "SELECT 1 FROM ValidPaths WHERE path = ? LIMIT 1"

QUERY_PATH_INFO = """
SELECT path, deriver, hash, registrationTime, narSize, ultimate, sigs, ca
FROM ValidPaths WHERE path = ?
"""

QUERY_REFERENCES = """
SELECT vp.path FROM Refs r
JOIN ValidPaths vp ON r.reference = vp.id
WHERE r.referrer = (SELECT id FROM ValidPaths WHERE path = ?)
"""

QUERY_PATH_FROM_HASH_PART = """
SELECT path FROM ValidPaths WHERE path >= ? AND path < ? LIMIT 1
"""

QUERY_ALL_VALID_PATHS = "SELECT path FROM ValidPaths"

QUERY_VALID_PATHS_BATCH = (
    "SELECT path FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))"
)

QUERY_STALE_PATHS = """
SELECT path FROM ValidPaths
WHERE registrationTime > 0 AND registrationTime < ?
"""

# Recursive CTE: expand seed paths to their full runtime reference closure
# and return metadata including references for all paths in the closure.
QUERY_CLOSURE_WITH_INFO = """
WITH RECURSIVE closure(id) AS (
    SELECT id FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))
    UNION
    SELECT r.reference
    FROM closure c
    JOIN Refs r ON c.id = r.referrer
)
SELECT vp.path, vp.deriver, vp.hash, vp.registrationTime, vp.narSize,
       vp.ultimate, vp.sigs, vp.ca,
       (SELECT group_concat(ref_vp.path, ' ')
        FROM Refs r
        JOIN ValidPaths ref_vp ON r.reference = ref_vp.id
        WHERE r.referrer = vp.id)
FROM closure c
JOIN ValidPaths vp ON c.id = vp.id
ORDER BY vp.id ASC
"""

# Batch query: PathInfo for multiple paths at once
QUERY_PATH_INFOS_BATCH = """
SELECT vp.path, vp.deriver, vp.hash, vp.registrationTime, vp.narSize,
       vp.ultimate, vp.sigs, vp.ca
FROM ValidPaths vp
WHERE vp.path IN (SELECT value FROM json_each(?))
"""

# Batch references: all (referrer_path, reference_path) pairs for a set of paths
QUERY_REFERENCES_BATCH = """
SELECT vp_referrer.path, vp_ref.path
FROM Refs r
JOIN ValidPaths vp_referrer ON r.referrer = vp_referrer.id
JOIN ValidPaths vp_ref ON r.reference = vp_ref.id
WHERE vp_referrer.path IN (SELECT value FROM json_each(?))
"""

# Batch outputs: all (drv_path, output_name, output_path) for a set of .drv files
QUERY_DERIVATION_OUTPUTS_BATCH = """
SELECT vp_drv.path, do.id, do.path
FROM DerivationOutputs do
JOIN ValidPaths vp_drv ON do.drv = vp_drv.id
WHERE vp_drv.path IN (SELECT value FROM json_each(?))
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

_DEFAULT_REGTIME_FLUSH_INTERVAL = 5.0


class LocalStoreDB:
    """Connection pool and dispatcher for Nix store SQLite.

    Operation types implement their own DB logic via ``execute_db(db)``.
    This class only manages connections and dispatches.

    Use the async factory ``await LocalStoreDB.open(store_path)`` to create.
    """

    def __init__(
        self,
        db_path: Path | None,
        read_only: bool,
        regtime_flush_interval: float,
        max_conns: int = 8,
    ) -> None:
        self.db_path = db_path
        self.read_only: bool = read_only
        self.regtime_flush_interval = regtime_flush_interval

        self.pending_regtime: set[StorePath] = set()
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
                async with self._pool_lock:
                    self._all_conns.append(conn)

            yield conn
        finally:
            if conn is not None:
                async with self._pool_lock:
                    self._idle_conns.append(conn)
            self._sem.release()

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
                read_only=True,
                regtime_flush_interval=regtime_flush_interval,
            )

        db_dir = db_path.parent
        can_write = os.access(db_dir, os.W_OK)
        read_only = not can_write

        try:
            instance = cls(
                db_path=db_path,
                read_only=read_only,
                regtime_flush_interval=regtime_flush_interval,
            )

            async with instance.acquire_conn() as db:
                if not read_only:
                    await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("SELECT 1 FROM ValidPaths LIMIT 1")

            log.info(
                "local_store_db_active",
                db_path=db_path,
                mode="read-write" if not read_only else "read-only",
            )
            return instance
        except Exception as e:
            log.warning(
                "nix_db_open_failed",
                db_path=db_path,
                error=e,
            )
            return cls(
                db_path=None,
                read_only=True,
                regtime_flush_interval=regtime_flush_interval,
            )

    # ── Dispatcher ────────────────────────────────────────────────────

    async def execute(self, request: OpRequest[Resp]) -> Resp | None:
        """Dispatch a request to its DB handler.

        Returns the response if the DB is active and the query succeeded,
        or None if the DB is unavailable (caller should fall back to wire).
        """
        if not self.active:
            return None
        try:
            return await request.execute_db(self)
        except Exception:
            log.debug(
                "db_execute_failed", request=type(request).__name__, exc_info=True
            )
            return None

    # ── Internal utility queries ──────────────────────────────────────
    # These are not operation dispatches but internal helpers used by
    # non-operation code (GC, build planner, http cache).

    async def query_stale_paths(self, max_age_seconds: int) -> set[StorePath] | None:
        """Find paths with registrationTime older than max_age_seconds ago."""
        if not self.active:
            return None
        try:
            cutoff = int(time.time()) - max_age_seconds
            async with self.acquire_conn() as conn:
                async with conn.execute(QUERY_STALE_PATHS, (cutoff,)) as cursor:
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

    def mark_paths(self, paths: set[StorePath]) -> None:
        """Queue multiple paths for registration time update."""
        if self.active and not self.read_only:
            self.pending_regtime.update(paths)

    async def flush_regtime(self) -> None:
        """Flush pending registration time updates to SQLite."""
        if not self.active or self.read_only or not self.pending_regtime:
            return

        import json

        paths = self.pending_regtime
        self.pending_regtime = set()

        try:
            paths_json = json.dumps(list(paths))
            t0 = time.monotonic()
            async with self.acquire_conn() as db:
                await db.execute(UPDATE_REGTIME, (paths_json,))
                await db.commit()
            elapsed = time.monotonic() - t0
            log.debug(
                "registration_time_updated",
                seed_count=len(paths),
                elapsed_ms=elapsed * 1000,
            )
        except Exception:
            log.exception("registration_time_update_failed")
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
                await self.flush_regtime()
        except asyncio.CancelledError:
            await self.flush_regtime()

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def close(self) -> None:
        """Stop flush task, flush pending writes, close database."""
        if self.flush_task is not None:
            self.flush_task.cancel()
            try:
                await self.flush_task
            except asyncio.CancelledError:
                pass
            self.flush_task = None
        await self.flush_regtime()
        await self.close_db_pool()

    async def close_db_pool(self) -> None:
        async with self._pool_lock:
            for db in self._all_conns:
                try:
                    await db.close()
                except Exception:
                    pass
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
