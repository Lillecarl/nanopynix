"""Direct SQLite access to the local Nix store database.

Provides two capabilities:

1. **Fast read queries** — IsValidPath, QueryPathInfo, QueryValidPaths,
   QueryAllValidPaths, QueryPathFromHashPart served directly from SQLite
   instead of round-tripping through the daemon protocol.

2. **Registration time updates** — Batched writes using Lix's recursive CTE
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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import structlog

from .operations.base import PathInfo, StringSetResponse
from .operations.queries import (
    IsValidPathResponse,
    QueryPathInfoResponse,
)
from .store_path import StorePath

log = structlog.get_logger(__name__)

# ── SQL ───────────────────────────────────────────────────────────────

_IS_VALID_PATH = "SELECT 1 FROM ValidPaths WHERE path = ? LIMIT 1"

_QUERY_PATH_INFO = """
SELECT path, deriver, hash, registrationTime, narSize, ultimate, sigs, ca
FROM ValidPaths WHERE path = ?
"""

_QUERY_REFERENCES = """
SELECT vp.path FROM Refs r
JOIN ValidPaths vp ON r.reference = vp.id
WHERE r.referrer = (SELECT id FROM ValidPaths WHERE path = ?)
"""

_QUERY_PATH_FROM_HASH_PART = """
SELECT path FROM ValidPaths WHERE path >= ? AND path < ? LIMIT 1
"""

_QUERY_ALL_VALID_PATHS = "SELECT path FROM ValidPaths"

_QUERY_VALID_PATHS_BATCH = (
    "SELECT path FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))"
)

_QUERY_STALE_PATHS = """
SELECT path FROM ValidPaths
WHERE registrationTime > 0 AND registrationTime < ?
"""

# Recursive CTE: expand seed paths to their full runtime reference closure
# and return metadata including references for all paths in the closure.
_QUERY_CLOSURE_WITH_INFO = """
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
_QUERY_PATH_INFOS_BATCH = """
SELECT vp.path, vp.deriver, vp.hash, vp.registrationTime, vp.narSize,
       vp.ultimate, vp.sigs, vp.ca
FROM ValidPaths vp
WHERE vp.path IN (SELECT value FROM json_each(?))
"""

# Batch references: all (referrer_path, reference_path) pairs for a set of paths
_QUERY_REFERENCES_BATCH = """
SELECT vp_referrer.path, vp_ref.path
FROM Refs r
JOIN ValidPaths vp_referrer ON r.referrer = vp_referrer.id
JOIN ValidPaths vp_ref ON r.reference = vp_ref.id
WHERE vp_referrer.path IN (SELECT value FROM json_each(?))
"""

# Lix's UpdateRegistrationTimeRecursive — walks the full closure
_UPDATE_REGTIME = """
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
    """Direct aiosqlite interface to a Nix store's database.

    Provides fast reads and batched registrationTime writes.
    All methods are safe to call even when the DB is unavailable —
    read methods return None, write methods silently no-op.

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
                # Create new connection
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
            # Create instance first
            instance = cls(
                db_path=db_path,
                read_only=read_only,
                regtime_flush_interval=regtime_flush_interval,
            )

            # Open one connection to verify and set WAL
            async with instance.acquire_conn() as db:
                if not read_only:
                    await db.execute("PRAGMA journal_mode=WAL")
                # Sanity check
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

    # ── Read queries ──────────────────────────────────────────────────

    async def is_valid_path(self, path: StorePath) -> IsValidPathResponse | None:
        """Check if a path exists. Returns None if DB unavailable."""
        if not self.active:
            return None
        try:
            async with self.acquire_conn() as db:
                async with db.execute(_IS_VALID_PATH, (path,)) as cursor:
                    row = await cursor.fetchone()
            return IsValidPathResponse(valid=row is not None)
        except Exception:
            log.debug("is_valid_path_query_failed", path=path, exc_info=True)
            return None

    async def query_path_info(self, path: StorePath) -> QueryPathInfoResponse | None:
        """Get full path info. Returns None if DB unavailable."""
        if not self.active:
            return None
        try:
            async with self.acquire_conn() as db:
                async with db.execute(_QUERY_PATH_INFO, (path,)) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    return QueryPathInfoResponse(valid=False)

                _path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca = row

                # Fetch references
                async with db.execute(_QUERY_REFERENCES, (path,)) as cursor:
                    ref_rows = await cursor.fetchall()
                refs = {r[0] for r in ref_rows}

            return QueryPathInfoResponse(
                valid=True,
                info=PathInfo(
                    path=path,
                    deriver=StorePath(deriver or ""),
                    nar_hash=nar_hash,
                    references={StorePath(r) for r in refs},
                    registration_time=reg_time,
                    nar_size=nar_size or 0,
                    ultimate=1 if ultimate else 0,
                    sigs=set(sigs.split()) if sigs else set(),
                    ca=ca or "",
                ),
            )
        except Exception:
            log.debug("query_path_info_failed", path=path, exc_info=True)
            return None

    async def query_valid_paths(
        self, paths: set[StorePath]
    ) -> StringSetResponse | None:
        """Filter a set of paths to those that exist. Returns None if DB unavailable."""
        if not self.active:
            return None
        try:
            paths_json = json.dumps(list(paths))
            async with self.acquire_conn() as db:
                async with db.execute(
                    _QUERY_VALID_PATHS_BATCH, (paths_json,)
                ) as cursor:
                    rows = await cursor.fetchall()
            return StringSetResponse(paths={StorePath(row[0]) for row in rows})
        except Exception:
            log.debug("query_valid_paths_failed", exc_info=True)
            return None

    async def query_all_valid_paths(self) -> StringSetResponse | None:
        """Get all valid paths. Returns None if DB unavailable."""
        if not self.active:
            return None
        try:
            async with self.acquire_conn() as db:
                async with db.execute(_QUERY_ALL_VALID_PATHS) as cursor:
                    rows = await cursor.fetchall()
            return StringSetResponse(paths={StorePath(r[0]) for r in rows})
        except Exception:
            log.debug("query_all_valid_paths_failed", exc_info=True)
            return None

    async def query_path_from_hash_part(self, hash_part: StorePath) -> StorePath | None:
        """Find a path by its hash prefix. Returns path string or None."""
        if not self.active:
            return None
        try:
            # Match /nix/store/<hash_part>...
            prefix = f"/nix/store/{hash_part}"
            # Upper bound: increment last char for range query
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            log.debug(
                "db_query_path_from_hash_part",
                hash_part=hash_part,
                range_start=prefix,
                range_end=upper,
            )
            async with self.acquire_conn() as db:
                async with db.execute(
                    _QUERY_PATH_FROM_HASH_PART,
                    (prefix, upper),
                ) as cursor:
                    row = await cursor.fetchone()
            log.debug(
                "db_query_path_from_hash_part_result",
                hash_part=hash_part,
                result=row[0] if row else None,
            )
            return StorePath(row[0]) if row else None
        except Exception:
            log.debug(
                "query_path_from_hash_part failed", hash_part=hash_part, exc_info=True
            )
            return None

    async def query_closure_with_info(
        self, seeds: set[StorePath]
    ) -> list[PathInfo] | None:
        """Expand seed paths to their full closure and return PathInfo for all.

        Returns a topologically sorted list of PathInfo objects.
        """
        if not self.active or not seeds:
            return None
        try:
            seeds_json = json.dumps(list(seeds))
            async with self.acquire_conn() as db:
                async with db.execute(
                    _QUERY_CLOSURE_WITH_INFO, (seeds_json,)
                ) as cursor:
                    rows = await cursor.fetchall()

            # Create PathInfo objects (already sorted by SQLite)
            sorted_infos: list[PathInfo] = []
            for (
                path,
                deriver,
                nar_hash,
                reg_time,
                nar_size,
                ultimate,
                sigs,
                ca,
                refs_str,
            ) in rows:
                p = StorePath(path)
                references = (
                    {StorePath(r) for r in refs_str.split()} if refs_str else set()
                )
                sorted_infos.append(
                    PathInfo(
                        path=p,
                        deriver=StorePath(deriver or ""),
                        nar_hash=nar_hash,
                        references=references,
                        registration_time=reg_time,
                        nar_size=nar_size or 0,
                        ultimate=1 if ultimate else 0,
                        sigs=set(sigs.split()) if sigs else set(),
                        ca=ca or "",
                    )
                )

            return sorted_infos
        except Exception:
            log.debug("query_closure_with_info_failed", exc_info=True)
            return None

    async def query_path_infos(
        self, paths: set[StorePath]
    ) -> dict[StorePath, PathInfo] | None:
        """Batch PathInfo for multiple paths. Returns None if DB unavailable."""
        if not self.active or not paths:
            return None
        try:
            paths_json = json.dumps(list(paths))
            async with self.acquire_conn() as db:
                async with db.execute(_QUERY_PATH_INFOS_BATCH, (paths_json,)) as cursor:
                    rows = await cursor.fetchall()
                async with db.execute(_QUERY_REFERENCES_BATCH, (paths_json,)) as cursor:
                    ref_rows = await cursor.fetchall()

            # Build referrer -> {references} map
            refs_map: dict[StorePath, set[StorePath]] = {}
            for referrer, reference in ref_rows:
                refs_map.setdefault(referrer, set()).add(reference)

            infos: dict[StorePath, PathInfo] = {}
            for path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca in rows:
                p = StorePath(path)
                infos[p] = PathInfo(
                    path=p,
                    deriver=StorePath(deriver or ""),
                    nar_hash=nar_hash,
                    references={StorePath(r) for r in refs_map.get(path, set())},
                    registration_time=reg_time,
                    nar_size=nar_size or 0,
                    ultimate=1 if ultimate else 0,
                    sigs=set(sigs.split()) if sigs else set(),
                    ca=ca or "",
                )
            return infos
        except Exception:
            log.debug("query_path_infos_failed", exc_info=True)
            return None

    async def query_stale_paths(self, max_age_seconds: int) -> set[StorePath] | None:
        """Find paths with registrationTime older than max_age_seconds ago.

        Returns None if DB unavailable.
        """
        if not self.active:
            return None
        try:
            cutoff = int(time.time()) - max_age_seconds
            async with self.acquire_conn() as db:
                async with db.execute(_QUERY_STALE_PATHS, (cutoff,)) as cursor:
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

        paths = self.pending_regtime
        self.pending_regtime = set()

        try:
            paths_json = json.dumps(list(paths))
            t0 = time.monotonic()
            async with self.acquire_conn() as db:
                await db.execute(_UPDATE_REGTIME, (paths_json,))
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
            # Also nullify db_path to prevent further acquisitions
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
