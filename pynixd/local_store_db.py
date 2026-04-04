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
from pathlib import Path

import aiosqlite
import structlog

from .operations.base import PathInfo, StringSetResponse
from .operations.queries import (
    IsValidPathResponse,
    QueryPathInfoResponse,
)

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
_COMPUTE_CLOSURE = """
WITH RECURSIVE closure(id, path) AS (
    SELECT id, path FROM ValidPaths WHERE path IN (SELECT value FROM json_each(?))
    UNION
    SELECT vp.id, vp.path
    FROM closure c
    JOIN Refs r ON c.id = r.referrer
    JOIN ValidPaths vp ON r.reference = vp.id
)
SELECT path FROM closure
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
        db: aiosqlite.Connection | None,
        read_only: bool,
        regtime_flush_interval: float,
    ) -> None:
        self._db: aiosqlite.Connection | None = db
        self._pending_regtime: set[str] = set()
        self._flush_task: asyncio.Task[None] | None = None
        self._regtime_flush_interval = regtime_flush_interval
        self._read_only: bool = read_only

    @classmethod
    async def open(
        cls,
        store_path: Path,
        regtime_flush_interval: float = _DEFAULT_REGTIME_FLUSH_INTERVAL,
    ) -> LocalStoreDB:
        """Open the Nix store database. Returns an instance (possibly with no DB)."""
        db_path = _resolve_db_path(store_path)
        if db_path is None:
            return cls(
                db=None, read_only=True, regtime_flush_interval=regtime_flush_interval
            )

        db_dir = db_path.parent
        can_write = os.access(db_dir, os.W_OK)
        read_only = not can_write

        try:
            mode = "ro" if read_only else "rw"
            uri = f"file:{db_path}?mode={mode}"
            db = await aiosqlite.connect(uri, uri=True)
            if not read_only:
                await db.execute("PRAGMA journal_mode=WAL")
            # Sanity check
            await db.execute("SELECT 1 FROM ValidPaths LIMIT 1")
            log.info(
                "local_store_db_active",
                db_path=db_path,
                mode="read-write" if not read_only else "read-only",
            )
            return cls(
                db=db,
                read_only=read_only,
                regtime_flush_interval=regtime_flush_interval,
            )
        except Exception as e:
            log.warning(
                "nix_db_open_failed",
                db_path=db_path,
                error=e,
            )
            return cls(
                db=None, read_only=True, regtime_flush_interval=regtime_flush_interval
            )

    # ── Read queries ──────────────────────────────────────────────────

    async def is_valid_path(self, path: str) -> IsValidPathResponse | None:
        """Check if a path exists. Returns None if DB unavailable."""
        if self._db is None:
            return None
        try:
            async with self._db.execute(_IS_VALID_PATH, (path,)) as cursor:
                row = await cursor.fetchone()
            return IsValidPathResponse(valid=row is not None)
        except Exception:
            log.debug("is_valid_path_query_failed", path=path, exc_info=True)
            return None

    async def query_path_info(self, path: str) -> QueryPathInfoResponse | None:
        """Get full path info. Returns None if DB unavailable."""
        if self._db is None:
            return None
        try:
            async with self._db.execute(_QUERY_PATH_INFO, (path,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return QueryPathInfoResponse(valid=False)

            _path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca = row

            # Fetch references
            async with self._db.execute(_QUERY_REFERENCES, (path,)) as cursor:
                ref_rows = await cursor.fetchall()
            refs = {r[0] for r in ref_rows}

            return QueryPathInfoResponse(
                valid=True,
                info=PathInfo(
                    path=path,
                    deriver=deriver or "",
                    nar_hash=nar_hash,
                    references=refs,
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

    async def query_valid_paths(self, paths: set[str]) -> StringSetResponse | None:
        """Filter a set of paths to those that exist. Returns None if DB unavailable."""
        if self._db is None:
            return None
        try:
            paths_json = json.dumps(list(paths))
            async with self._db.execute(
                _QUERY_VALID_PATHS_BATCH, (paths_json,)
            ) as cursor:
                rows = await cursor.fetchall()
            return StringSetResponse(paths={row[0] for row in rows})
        except Exception:
            log.debug("query_valid_paths_failed", exc_info=True)
            return None

    async def query_all_valid_paths(self) -> StringSetResponse | None:
        """Get all valid paths. Returns None if DB unavailable."""
        if self._db is None:
            return None
        try:
            async with self._db.execute(_QUERY_ALL_VALID_PATHS) as cursor:
                rows = await cursor.fetchall()
            return StringSetResponse(paths={r[0] for r in rows})
        except Exception:
            log.debug("query_all_valid_paths_failed", exc_info=True)
            return None

    async def query_path_from_hash_part(self, hash_part: str) -> str | None:
        """Find a path by its hash prefix. Returns path string or None."""
        if self._db is None:
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
            async with self._db.execute(
                _QUERY_PATH_FROM_HASH_PART,
                (prefix, upper),
            ) as cursor:
                row = await cursor.fetchone()
            log.debug(
                "db_query_path_from_hash_part_result",
                hash_part=hash_part,
                result=row[0] if row else None,
            )
            return row[0] if row else None
        except Exception:
            log.debug(
                "query_path_from_hash_part failed", hash_part=hash_part, exc_info=True
            )
            return None

    async def compute_closure(self, seeds: set[str]) -> set[str] | None:
        """Expand seed paths to their full runtime reference closure.

        Single recursive CTE — no per-path queries. Returns None if DB unavailable.
        """
        if self._db is None or not seeds:
            return None
        try:
            seeds_json = json.dumps(list(seeds))
            async with self._db.execute(_COMPUTE_CLOSURE, (seeds_json,)) as cursor:
                rows = await cursor.fetchall()
            return {r[0] for r in rows}
        except Exception:
            log.debug("compute_closure_failed", exc_info=True)
            return None

    async def query_path_infos(self, paths: set[str]) -> dict[str, PathInfo] | None:
        """Batch PathInfo for multiple paths. Returns None if DB unavailable."""
        if self._db is None or not paths:
            return None
        try:
            paths_json = json.dumps(list(paths))
            async with self._db.execute(
                _QUERY_PATH_INFOS_BATCH, (paths_json,)
            ) as cursor:
                rows = await cursor.fetchall()
            async with self._db.execute(
                _QUERY_REFERENCES_BATCH, (paths_json,)
            ) as cursor:
                ref_rows = await cursor.fetchall()

            # Build referrer -> {references} map
            refs_map: dict[str, set[str]] = {}
            for referrer, reference in ref_rows:
                refs_map.setdefault(referrer, set()).add(reference)

            infos: dict[str, PathInfo] = {}
            for path, deriver, nar_hash, reg_time, nar_size, ultimate, sigs, ca in rows:
                infos[path] = PathInfo(
                    path=path,
                    deriver=deriver or "",
                    nar_hash=nar_hash,
                    references=refs_map.get(path, set()),
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

    async def query_stale_paths(self, max_age_seconds: int) -> set[str] | None:
        """Find paths with registrationTime older than max_age_seconds ago.

        Returns None if DB unavailable.
        """
        if self._db is None:
            return None
        try:
            cutoff = int(time.time()) - max_age_seconds
            async with self._db.execute(_QUERY_STALE_PATHS, (cutoff,)) as cursor:
                rows = await cursor.fetchall()
            return {r[0] for r in rows}
        except Exception:
            log.debug("query_stale_paths_failed", exc_info=True)
            return None

    # ── Registration time updates ─────────────────────────────────────

    def mark_path(self, path: str) -> None:
        """Queue a path for registration time update."""
        if self._db is not None and not self._read_only:
            log.debug("db_mark_path", path=path)
            self._pending_regtime.add(path)

    def mark_paths(self, paths: set[str]) -> None:
        """Queue multiple paths for registration time update."""
        if self._db is not None and not self._read_only:
            self._pending_regtime.update(paths)

    async def flush_regtime(self) -> None:
        """Flush pending registration time updates to SQLite."""
        if self._db is None or self._read_only or not self._pending_regtime:
            return

        paths = self._pending_regtime
        self._pending_regtime = set()

        try:
            paths_json = json.dumps(list(paths))
            t0 = time.monotonic()
            await self._db.execute(_UPDATE_REGTIME, (paths_json,))
            await self._db.commit()
            elapsed = time.monotonic() - t0
            log.debug(
                "registration_time_updated",
                seed_count=len(paths),
                elapsed_ms=elapsed * 1000,
            )
        except Exception:
            log.exception("registration_time_update_failed")
            await self._close_db()

    def start(self) -> None:
        """Start background regtime flush task. Call from async context."""
        if self._db is None or self._flush_task is not None or self._read_only:
            return
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._regtime_flush_interval)
                await self.flush_regtime()
        except asyncio.CancelledError:
            await self.flush_regtime()

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def close(self) -> None:
        """Stop flush task, flush pending writes, close database."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush_regtime()
        await self._close_db()

    async def _close_db(self) -> None:
        db = self._db
        self._db = None
        if db is not None:
            try:
                await db.close()
            except Exception:
                pass


def _resolve_db_path(store_path: Path) -> Path | None:
    """Compute path to db.sqlite for a given store root."""
    if store_path == Path("/") or not store_path:
        db_path = Path("/nix/var/nix/db/db.sqlite")
    else:
        db_path = store_path / "nix" / "var" / "nix" / "db" / "db.sqlite"

    if not db_path.exists():
        log.warning("nix_db_not_found", db_path=db_path)
        return None
    return db_path
