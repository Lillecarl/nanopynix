"""
Store ABC and concrete types for build machines.

A Store manages on-demand Connection connections to a single build machine,
handling connection pooling, concurrency limiting, and idle TTL cleanup.
Each Store type handles transport setup (subprocess, SSH channel, socket)
and constructs Connection instances with the resulting reader/writer pair.

Store types:
- LocalSocketStore: connects to local nix-daemon Unix socket
- SSHSubprocessStore: persistent SSH, nix-daemon --stdio channels
- SSHSocketStore: persistent SSH, Unix socket tunnels
"""

from __future__ import annotations

import asyncio
import os
import platform
import shlex
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

import asyncssh
import structlog
from cachetools import TTLCache
from environs import env

from . import wire
from .connection import ClientConn, Connection
from .local_store_db import LocalStoreDB
from .operations.add_multiple_to_store import AddMultipleToStoreRequest
from .operations.base import (
    OpRequest,
    PathInfo,
    Resp,
)
from .operations.query_closure_with_info import QueryClosureWithInfoRequest
from .psi import MemInfo, PsiSnapshot, parse_meminfo, parse_psi_output
from .signing import SecretKey
from .store_path import RequiredInput, StorePath
from .wire import (
    SSHNixReader,
    SSHNixWriter,
    UnixNixReader,
    UnixNixWriter,
)

log = structlog.get_logger(__name__)
pool_log = structlog.get_logger(f"{__name__}.pool")


_DEFAULT_IDLE_TTL: float = 10.0
_CB_THRESHOLD: int = 3  # failures before cooldown
_CB_MAX_COOLDOWN: float = 300.0  # 5 min max

# Per-store connection holder tracking via ContextVar
# Tracks nested connection count per (store_id, kind) tuple
_nested_conns: ContextVar[dict[tuple[str, str], int]] = ContextVar(
    "_nested_conns",
    default={},  # type: ignore[arg-type]
)


class Store(ABC):
    """A build store with on-demand connection pooling.

    Subclasses implement create_conn() to set up transport and
    return a connected Connection. The base class handles pooling,
    concurrency limiting, and idle TTL cleanup.

    Idle connections are automatically closed after idle_ttl seconds.
    """

    def __init__(
        self,
        id: str,
        store_path: Path = Path("/"),
        max_builds: int = 2,
        max_transfers: int = 4,
        idle_ttl: float = _DEFAULT_IDLE_TTL,
        supported_systems: list[str] | None = None,
    ) -> None:
        self.id = id
        self.store_path = store_path
        self.version: int = wire.PROTOCOL_VERSION
        self.nix_version: str = ""
        self.max_builds = max_builds
        self.max_transfers = max_transfers
        self.idle_ttl = idle_ttl
        self.build_semaphore = asyncio.Semaphore(max_builds)
        self.transfer_semaphore = asyncio.Semaphore(max_transfers)
        self.idle_conns: list[tuple[Connection, float]] = []
        self.all_conns: list[Connection] = []
        self.conn_counter: int = 0
        self.sweep_task: asyncio.Task[None] | None = None
        self.supported_systems = supported_systems or []
        self.known_paths: set[RequiredInput] = set()
        self.path_info_cache: TTLCache[StorePath, PathInfo] = TTLCache(
            maxsize=10000, ttl=300
        )
        self.consecutive_failures: int = 0
        self.cooldown_until: float = 0.0
        self.db: LocalStoreDB | None = None
        self.supported_features: set[str] = set()
        self.probed = False
        self.signing_keys: dict[str, SecretKey] = {}
        self._holder_task: asyncio.Task | None = None

    @property
    def db_enabled(self) -> bool:
        """True if this store should use a local SQLite DB for metadata."""
        return True

    @property
    def signing_key_names(self) -> list[str]:
        """List of signing key names configured on this store."""
        return list(self.signing_keys.keys())

    def get_signing_key(self, name: str) -> SecretKey:
        """Get a signing key by name."""
        key = self.signing_keys.get(name)
        if key is None:
            raise KeyError(f"Signing key '{name}' not found")
        return key

    def supports_system(self, system: str) -> bool:
        """Check if this store supports the given system."""
        if not self.supported_systems:
            return True  # No restriction = supports all
        return system in self.supported_systems

    # ── Circuit breaker ──────────────────────────────────────────────

    @property
    def is_healthy(self) -> bool:
        """False while in cooldown. Becomes True when cooldown expires (half-open)."""
        return time.monotonic() >= self.cooldown_until

    def record_success(self) -> None:
        """Reset circuit breaker on successful operation."""
        if self.consecutive_failures > 0:
            log.info(
                "store_recovered",
                store_id=self.id,
                consecutive_failures=self.consecutive_failures,
            )
        self.consecutive_failures = 0
        self.cooldown_until = 0.0

    def record_failure(self) -> None:
        """Record a failure. After threshold, enter cooldown."""
        self.consecutive_failures += 1
        if self.consecutive_failures >= _CB_THRESHOLD:
            cooldown = min(
                30 * 2 ** (self.consecutive_failures - _CB_THRESHOLD),
                _CB_MAX_COOLDOWN,
            )
            self.cooldown_until = time.monotonic() + cooldown
            log.warning(
                "store_cooldown",
                store_id=self.id,
                consecutive_failures=self.consecutive_failures,
                cooldown=cooldown,
            )

    # ── Known paths tracking ────────────────────────────────────────

    def has_path(self, path: StorePath) -> bool:
        return path in self.known_paths

    def has_all_paths(self, paths: set[RequiredInput]) -> bool:
        return paths.issubset(self.known_paths)

    def count_common_paths(self, paths: set[RequiredInput]) -> int:
        return len(paths & self.known_paths)

    async def call(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
        raise_on_error: bool = False,
    ) -> Resp:
        """Execute any operation on this store. Handles connection lifecycle.

        Args:
            request: The operation request object.
            client: Optional client connection for stderr forwarding.
            suppress_last: If True, consume but don't forward STDERR_LAST.
            raise_on_error: Whether to raise BackendError on daemon errors.
        """
        # Use build_conn for builds, transfer_conn for queries/mutations
        if request.is_build:
            pool = self.build_conn
            pool_name = "build"
        else:
            pool = self.transfer_conn
            pool_name = "transfer"

        log.debug("acquiring_connection", pool=pool_name, op=request.__class__.__name__)
        async with pool() as conn:
            log.debug(
                "connection_acquired", pool=pool_name, op=request.__class__.__name__
            )
            res = await conn.call(
                request,
                client=client,
                suppress_last=suppress_last,
                raise_on_error=raise_on_error,
            )
            log.debug(
                "connection_releasing", pool=pool_name, op=request.__class__.__name__
            )
            return res

    async def execute(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> Resp:
        """Execute an operation on this store.

        Delegates logic to the request object, which may use fast-paths
        (SQLite, memory) or fallback to this store's 'call' method.
        """
        return await request.execute(
            self,
            client=client,
            suppress_last=suppress_last,
        )

    def add_known_path(self, path: StorePath, *, update_regtime: bool = True) -> None:
        self.known_paths.add(path)  # type: ignore[arg-type]
        if update_regtime and self.db is not None:
            self.db.mark_path(path)

    def add_known_paths(
        self, paths: set[StorePath], *, update_regtime: bool = True
    ) -> None:
        self.known_paths.update(paths)  # type: ignore[arg-type]
        if update_regtime and self.db is not None:
            self.db.mark_paths(set(paths))

    def add_path_info(self, info: PathInfo) -> None:
        """Add PathInfo to the cache."""
        self.path_info_cache[info.path] = info

    def add_path_infos(self, infos: Iterable[PathInfo]) -> None:
        """Add multiple PathInfos to the cache."""
        for info in infos:
            self.path_info_cache[info.path] = info

    def get_path_info(self, path: StorePath) -> PathInfo | None:
        """Get PathInfo from cache if available."""
        return self.path_info_cache.get(path)

    @classmethod
    async def stream_paths_with_info_store_to_store(
        cls,
        src: Store,
        dst: Store,
        infos: Iterable[PathInfo],
        src_conn: Connection | None = None,
        dst_conn: Connection | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Copy paths from src to dst via streaming using pre-resolved PathInfo.

        The infos should be topologically sorted to ensure valid insertion order
        at the destination.
        """
        infos_list = list(infos)
        if not infos_list:
            return

        if (src_conn or dst_conn) and src_conn == dst_conn:
            log.error("same_conn_transfer")
            return

        log.info(
            "stream_paths_with_info_store_to_store",
            count=len(infos_list),
        )

        if src_conn and dst_conn:
            await cls._stream_with_info_inner(
                src_conn, dst_conn, infos_list, cancel_event
            )
        elif src_conn:
            async with dst.transfer_conn() as dst_conn:
                await cls._stream_with_info_inner(
                    src_conn, dst_conn, infos_list, cancel_event
                )
        elif dst_conn:
            async with src.transfer_conn() as src_conn:
                await cls._stream_with_info_inner(
                    src_conn, dst_conn, infos_list, cancel_event
                )
        else:
            async with src.transfer_conn() as src_conn, dst.transfer_conn() as dst_conn:
                await cls._stream_with_info_inner(
                    src_conn, dst_conn, infos_list, cancel_event
                )

    @classmethod
    async def _stream_with_info_inner(
        cls,
        src_conn: Connection,
        dst_conn: Connection,
        infos_list: list[PathInfo],
        cancel_event: asyncio.Event | None,
    ) -> None:
        """Inner streaming logic assuming both connections are already acquired."""
        dst_conn.op_log.append(
            "AddMultipleToStore (stream_paths_with_info_store_to_store)"
        )
        req = AddMultipleToStoreRequest(
            repair=0,
            dont_check_sigs=1,
        )
        await req.to_writer(dst_conn.w, dst_conn.version)

        fw = dst_conn.w.framed()
        fw.write_uint64(len(infos_list))

        for info in infos_list:
            if cancel_event and cancel_event.is_set():
                log.info("stream_paths_transfer_cancelled")
                break

            path = info.path
            dst_conn.op_log.append(
                "AddToStoreNar (stream_paths_with_info_store_to_store)"
            )

            await info.to_writer_keyed(fw)

            from .operations.nar_from_path import NarFromPathRequest

            await NarFromPathRequest(path=path).to_writer(src_conn.w, src_conn.version)
            await src_conn.w.drain()
            await src_conn.r.drain_stderr()

            await wire.pipe_raw_to_framed_writer(
                src_conn.r,
                fw,
                info.nar_size,
            )

        await fw.finalize()
        await dst_conn.r.drain_stderr()
        await req.response_type.from_reader(dst_conn.r, dst_conn.version)

    @classmethod
    async def stream_paths_store_to_store(
        cls,
        src: Store,
        dst: Store,
        paths: Iterable[StorePath],
        src_conn: Connection | None = None,
        dst_conn: Connection | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Copy paths from src to dst via streaming, querying info first.

        Acquires connections once and reuses them for both metadata and data.
        """
        paths_set: set[StorePath] = {StorePath(p) for p in paths}
        if not paths_set:
            return

        if (src_conn or dst_conn) and src_conn == dst_conn:
            log.error("same_conn_transfer")
            return

        if src_conn and dst_conn:
            closure_resp = await src.execute(
                QueryClosureWithInfoRequest(paths=paths_set),
                client=None,
            )
            await cls._stream_with_info_inner(
                src_conn, dst_conn, list(closure_resp.infos), cancel_event
            )
        elif src_conn:
            async with dst.transfer_conn() as dst_conn:
                closure_resp = await src.execute(
                    QueryClosureWithInfoRequest(paths=paths_set),
                    client=None,
                )
                await cls._stream_with_info_inner(
                    src_conn, dst_conn, list(closure_resp.infos), cancel_event
                )
        elif dst_conn:
            async with src.transfer_conn() as src_conn:
                closure_resp = await src.execute(
                    QueryClosureWithInfoRequest(paths=paths_set),
                    client=None,
                )
                await cls._stream_with_info_inner(
                    src_conn, dst_conn, list(closure_resp.infos), cancel_event
                )
        else:
            async with src.transfer_conn() as src_conn, dst.transfer_conn() as dst_conn:
                closure_resp = await src.execute(
                    QueryClosureWithInfoRequest(paths=paths_set),
                    client=None,
                )
                await cls._stream_with_info_inner(
                    src_conn, dst_conn, list(closure_resp.infos), cancel_event
                )

    async def pipe_nar_from(
        self,
        src: Store,
        path: StorePath,
        info: PathInfo,
    ) -> None:
        """Stream NAR from src store to this store."""
        async with self.transfer_conn() as dst_conn, src.transfer_conn() as src_conn:
            from .operations.add_to_store_nar import AddToStoreNarRequest
            from .operations.nar_from_path import NarFromPathRequest

            await NarFromPathRequest(path=path).to_writer(src_conn.w, src_conn.version)
            await src_conn.w.drain()
            await src_conn.r.drain_stderr()

            nar_request = AddToStoreNarRequest(
                info=info,
                repair=0,
                dont_check_sigs=1,
            )
            await nar_request.to_writer(dst_conn.w, dst_conn.version)

            await wire.pipe_raw_to_framed(
                src_conn.r,
                dst_conn.w,
                info.nar_size,
            )

            await dst_conn.r.drain_stderr()
            await nar_request.response_type.from_reader(dst_conn.r, dst_conn.version)

    @property
    def available_transfer_slots(self) -> int:
        """Number of free transfer slots."""
        return self.transfer_semaphore._value

    @abstractmethod
    async def create_conn(self) -> Connection:
        """Create transport, construct Connection, and connect it."""
        ...

    async def probe_version(self) -> int:
        """Connect once to discover the daemon's protocol version.

        The connection is returned to the idle pool for reuse.
        """
        async with self.transfer_conn() as _conn:
            pass
        return self.version

    async def warm_pool(self, n: int) -> None:
        """Pre-create n connections and park them in the idle pool."""
        conns = await asyncio.gather(*[self.create_conn() for _ in range(n)])
        now = time.monotonic()
        for conn in conns:
            self.all_conns.append(conn)
            self.idle_conns.append((conn, now))
        self.start_sweep()
        log.info("pool_warmed", store_id=self.id, connections=n)

    @property
    def available_slots(self) -> int:
        """Number of free build slots."""
        return self.build_semaphore._value

    @property
    def pressure(self) -> float | None:
        """System pressure score (0-100), or None if unavailable."""
        return None

    @property
    def meminfo(self) -> MemInfo | None:
        """System memory info, or None if unavailable."""
        return None

    @property
    def in_flight(self) -> int:
        return self.max_builds - self.build_semaphore._value

    @property
    def is_lix(self) -> bool:
        """True if this store is Lix (protocol version 1.35 and version string)."""
        if self.version != wire.proto(1, 35):
            return False

        return "lix" in self.nix_version.lower()

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
                    pool_log.debug(
                        "pool_closing_expired_idle",
                        store_id=self.id,
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

    @staticmethod
    async def reader_is_dirty(conn: Connection) -> bool:
        """Check if the reader has unread buffered data (protocol desync)."""
        return await conn.r.is_dirty()

    async def get_or_create_conn(self) -> Connection:
        """Pop an idle connection or create a new one."""
        now = time.monotonic()
        while self.idle_conns:
            candidate, returned_at = self.idle_conns.pop()
            if now - returned_at >= self.idle_ttl:
                pool_log.debug(
                    "pool_discarding_expired",
                    store_id=self.id,
                    conn_id=candidate.id,
                )
                if candidate in self.all_conns:
                    self.all_conns.remove(candidate)
                try:
                    await candidate.close()
                except Exception:
                    pass
                continue
            if candidate.dirty or await self.reader_is_dirty(candidate):
                log.warning(
                    "pool_discarding_dirty_conn",
                    store_id=self.id,
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
            pool_log.debug(
                "pool_reusing_conn",
                store_id=self.id,
                conn_id=candidate.id,
            )
            return candidate

        self.conn_counter += 1
        conn = await self.create_conn()
        self.all_conns.append(conn)
        # Capture protocol version and nix version string from first connection
        if self.conn_counter == 1:
            self.version = conn.version
            self.nix_version = conn.nix_version
            self.supported_features = conn.features
            self.probed = True
            log.info(
                "store_protocol_version",
                store_id=self.id,
                version=wire.proto_str(self.version),
                nix_version=self.nix_version,
                features=sorted(self.supported_features),
            )
        pool_log.debug(
            "pool_created_connection",
            store_id=self.id,
            conn_id=conn.id,
            pool_stats=self.pool_stats,
        )
        return conn

    @asynccontextmanager
    async def acquire_conn(
        self,
        semaphore: asyncio.Semaphore,
    ) -> AsyncIterator[Connection]:
        """Acquire a connection from the shared pool.

        If the same task that is already holding a connection tries to
        acquire again (re-entry), a new connection is allocated outside
        the semaphore to avoid deadlock. A warning is logged for investigation.
        """
        kind = "build" if semaphore is self.build_semaphore else "transfer"
        key = (self.id, kind)
        counts = dict(_nested_conns.get())  # Copy to avoid race with nested code
        re_entrant = counts.get(key, 0) > 0 and semaphore.locked()
        if counts.get(key, 0) > 0:
            pool_log.warning(
                "store_reentrant_acquire",
                store_id=self.id,
                kind=kind,
            )

        if re_entrant:
            conn = await self.create_conn()
            self.all_conns.append(conn)
            counts[key] = counts.get(key, 0) + 1
            _nested_conns.set(counts)
            try:
                async with conn:
                    yield conn
            finally:
                counts = dict(_nested_conns.get())
                counts[key] = counts.get(key, 0) - 1
                _nested_conns.set(counts)
                if conn in self.all_conns:
                    self.all_conns.remove(conn)
                try:
                    await conn.close()
                except Exception:
                    pass
            return

        if semaphore.locked():
            limit = self.max_builds if kind == "build" else self.max_transfers
            pool_log.info(
                "pool_all_slots_in_use",
                store_id=self.id,
                limit=limit,
                kind=kind,
            )
        await semaphore.acquire()
        conn: Connection | None = None
        try:
            conn = await self.get_or_create_conn()
            counts = dict(_nested_conns.get())
            counts[key] = counts.get(key, 0) + 1
            _nested_conns.set(counts)
            async with conn:
                yield conn
        finally:
            counts = dict(_nested_conns.get())
            counts[key] = counts.get(key, 0) - 1
            _nested_conns.set(counts)
            if conn is not None:
                if conn.dirty:
                    log.warning(
                        "store_discarding_dirty_connection",
                        store_id=self.id,
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
            semaphore.release()

    def build_conn(self) -> AbstractAsyncContextManager[Connection]:
        """Acquire a build connection (counts against max_builds).."""
        return self.acquire_conn(self.build_semaphore)

    def transfer_conn(self) -> AbstractAsyncContextManager[Connection]:
        """Acquire a transfer connection (counts against max_transfers).

        Transfer connections share the same pool as build connections
        but use a separate semaphore so transfers don't block builds.
        """
        return self.acquire_conn(self.transfer_semaphore)

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

    @property
    def pool_stats(self) -> str:
        """Human-readable pool statistics."""
        build_in_use = self.max_builds - self.build_semaphore._value
        transfer_in_use = self.max_transfers - self.transfer_semaphore._value
        return (
            f"builds={build_in_use}/{self.max_builds} "
            f"transfers={transfer_in_use}/{self.max_transfers} "
            f"idle={len(self.idle_conns)} total={len(self.all_conns)}"
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(id={self.id!r}, "
            f"in_flight={self.in_flight}/{self.max_builds}, "
            f"idle={len(self.idle_conns)}, "
            f"connections={len(self.all_conns)})"
        )


class _SSHStoreMixin:
    """Shared SSH connection management with exponential backoff reconnection.

    Subclasses must set host, port, username on __init__.
    """

    host: str
    port: int
    username: str | None
    client_keys: list[str | Path | asyncssh.SSHKey] | None
    conn: asyncssh.SSHClientConnection | None
    backoff: float
    max_backoff: float
    last_failure: float
    id: str

    INITIAL_BACKOFF: float = 1.0
    MAX_BACKOFF: float = 60.0
    PSI_INTERVAL = env.float("PYNIXD_PSI_INTERVAL", 5.0)

    def init_ssh_state(
        self,
        *,
        monitor: bool = True,
        client_keys: list[str | Path | asyncssh.SSHKey] | None = None,
    ) -> None:
        self.conn = None
        self.ssh_lock = asyncio.Lock()
        self.backoff = self.INITIAL_BACKOFF
        self.max_backoff = self.MAX_BACKOFF
        self.last_failure = 0.0
        self.monitor_enabled = monitor
        self.client_keys = client_keys
        self.psi_data: PsiSnapshot | None = None
        self.meminfo_data: MemInfo | None = None
        self.psi_task: asyncio.Task[None] | None = None

    def start_psi_polling(self) -> None:
        """Start PSI polling loop. Called after first successful SSH connect."""
        if not self.monitor_enabled:
            return
        if self.psi_task is None or self.psi_task.done():
            self.psi_task = asyncio.create_task(self.psi_poll_loop())

    PSI_FILES: tuple[str, ...] = (
        "/proc/pressure/cpu",
        "/proc/pressure/memory",
        "/proc/pressure/io",
    )

    async def psi_poll_loop(self) -> None:
        """Periodically read PSI and meminfo data over SFTP."""
        while True:
            try:
                conn = self.conn
                if conn is None:
                    await asyncio.sleep(self.PSI_INTERVAL)
                    continue
                async with conn.start_sftp_client() as sftp:
                    while True:
                        parts = []
                        for path in self.PSI_FILES:
                            async with sftp.open(path, "r") as f:
                                parts.append(await f.read())
                        self.psi_data = parse_psi_output("".join(parts))
                        try:
                            async with sftp.open("/proc/meminfo", "r") as f:
                                self.meminfo_data = parse_meminfo(await f.read())
                        except asyncssh.SFTPError:
                            pass  # meminfo optional
                        await asyncio.sleep(self.PSI_INTERVAL)
            except asyncio.CancelledError:
                return
            except (
                asyncssh.SFTPNoSuchFile,
                asyncssh.SFTPPermissionDenied,
                asyncssh.SFTPOpUnsupported,
            ) as e:
                # PSI not available on this host (macOS, old kernel, restricted perms)
                log.info("psi_unavailable", store_id=self.id, error=e)
                self.psi_data = None
                return
            except asyncssh.SFTPConnectionLost:
                # SFTP channel died, retry after SSH reconnects
                log.debug("psi_sftp_lost", store_id=self.id)
                self.psi_data = None
                await asyncio.sleep(self.PSI_INTERVAL)
            except asyncssh.SFTPError as e:
                # Any other SFTP error — probably not recoverable
                log.info("psi_sftp_error", store_id=self.id, error=str(e))
                self.psi_data = None
                return
            except (asyncssh.Error, OSError) as e:
                # SSH connection-level error — retry, SSH reconnect may fix it
                log.debug("psi_ssh_error", store_id=self.id, error=str(e))
                self.psi_data = None
                await asyncio.sleep(self.PSI_INTERVAL)

    def stop_psi_polling(self) -> None:
        """Cancel the PSI polling task."""
        if self.psi_task is not None:
            self.psi_task.cancel()
            self.psi_task = None

    @property
    def pressure(self) -> float | None:
        """System pressure score (0-100), or None if unavailable."""
        if self.psi_data is None:
            return None
        # Stale check: 3x interval
        if time.monotonic() - self.psi_data.timestamp > self.PSI_INTERVAL * 3:
            return None
        return self.psi_data.pressure_score()

    @property
    def meminfo(self) -> MemInfo | None:
        """System memory info, or None if unavailable."""
        return self.meminfo_data

    async def ensure_ssh(self) -> asyncssh.SSHClientConnection:
        if self.conn is not None:
            return self.conn

        async with self.ssh_lock:
            # Re-check after acquiring lock (another task may have connected)
            if self.conn is not None:
                return self.conn

            # Respect backoff from previous failure
            now = time.monotonic()
            wait = self.last_failure + self.backoff - now
            if self.last_failure > 0 and wait > 0:
                log.info("ssh_backoff", store_id=self.id, backoff_seconds=wait)
                await asyncio.sleep(wait)

            try:
                log.info(
                    "ssh_connecting",
                    username=self.username or "",
                    host=self.host,
                    port=self.port,
                )
                self.conn = await asyncssh.connect(
                    self.host,
                    port=self.port,
                    username=self.username,
                    client_keys=self.client_keys,
                    known_hosts=None,
                )
                # Reset backoff on success
                self.backoff = self.INITIAL_BACKOFF
                self.last_failure = 0.0
                self.record_success()  # type: ignore[attr-defined]
                self.start_psi_polling()
                return self.conn
            except Exception:
                self.last_failure = time.monotonic()
                self.backoff = min(self.backoff * 2, self.MAX_BACKOFF)
                self.record_failure()  # type: ignore[attr-defined]
                log.warning(
                    "ssh_connect_failed",
                    store_id=self.id,
                    next_retry_seconds=self.backoff,
                )
                raise

    def invalidate_ssh(self) -> None:
        """Mark SSH connection as dead so next ensure_ssh reconnects."""
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    async def close_ssh(self) -> None:
        self.stop_psi_polling()
        if self.conn is not None:
            self.conn.close()
            self.conn = None


class SSHSubprocessStore(_SSHStoreMixin, Store):
    """Persistent SSH connection, spawns nix-daemon --stdio channels.

    Used primarily for "fake Nix" stores like nixbuild.net that provide
    a nix-daemon protocol over stdin/stdout. For real Nix stores over SSH,
    SSHSocketStore (tunnelling to a Unix socket) is preferred.

    If store_path is set, runs ``nix daemon --store <path> --stdio``.
    Otherwise runs ``nix-daemon --stdio`` (default store, nixbuild.net compat).
    """

    def __init__(
        self,
        host: str,
        id: str | None = None,
        port: int = 22,
        username: str | None = None,
        store_path: Path = Path("/"),
        max_builds: int = 2,
        max_transfers: int = 4,
        supported_systems: list[str] | None = None,
        monitor: bool = True,
        client_keys: list[str | Path | asyncssh.SSHKey] | None = None,
        nix_bin: str = "nix",
    ) -> None:
        super().__init__(
            id=id or f"ssh:{username or ''}@{host}:{port}",
            store_path=store_path,
            max_builds=max_builds,
            max_transfers=max_transfers,
            supported_systems=supported_systems,
        )
        self.host = host
        self.port = port
        self.username = username
        self.nix_bin = nix_bin
        self.init_ssh_state(monitor=monitor, client_keys=client_keys)
        self.ssh_processes: list[asyncssh.SSHClientProcess] = []

    async def create_conn(self) -> Connection:
        try:
            ssh_conn = await self.ensure_ssh()
        except Exception:
            raise
        conn_id = f"{self.id}-{self.conn_counter}"
        if self.store_path and self.store_path != Path("/"):
            cmd = f"{self.nix_bin} daemon --store {self.store_path} --stdio"
        elif self.nix_bin != "nix":
            cmd = f"{self.nix_bin} daemon --stdio"
        else:
            cmd = "nix-daemon --stdio"
        log.debug(
            "spawning_remote_daemon",
            cmd=cmd,
            conn_id=conn_id,
        )
        try:
            proc = await ssh_conn.create_process(cmd, encoding=None)
        except Exception:
            self.invalidate_ssh()
            raise
        self.ssh_processes.append(proc)
        proc.channel.set_write_buffer_limits(
            high=wire._SSH_WINDOW_SIZE, low=wire._SSH_WINDOW_SIZE // 4
        )

        conn = Connection(SSHNixReader(proc.stdout), SSHNixWriter(proc.stdin), conn_id)
        await conn.connect()
        return conn

    async def close(self) -> None:
        """Close stores, SSH processes, and SSH connection."""
        await super().close()
        for proc in self.ssh_processes:
            try:
                proc.terminate()
            except Exception:
                pass
            proc.close()
        self.ssh_processes.clear()
        await self.close_ssh()


class LocalSocketStore(Store):
    """Connects to local nix-daemon via Unix socket.

    Can optionally spawn and manage its own daemon subprocess with a
    custom --store path. The socket is placed at
    ``<store_path>/var/nix/daemon-socket/socket`` and the daemon is
    told about it via NIX_DAEMON_SOCKET_PATH.

    If no store_path is given (or store_path="/"), connects to the
    system daemon socket without spawning anything.
    """

    def __init__(
        self,
        id: str | None = None,
        store_path: Path | None = None,
        socket_path: Path | None = None,
        max_builds: int = 1,
        max_transfers: int = 4,
        supported_systems: list[str] | None = None,
        nix_bin: str = "nix",
        extra_env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
        use_db: bool = True,
    ) -> None:
        if store_path is None:
            store_path = Path("/")
        managed = store_path != Path("/")
        if socket_path:
            self.socket_path = socket_path
        elif managed:
            self.socket_path = store_path / "var" / "nix" / "daemon-socket" / "socket"
        else:
            self.socket_path = DAEMON_SOCKET_PATH

        super().__init__(
            id=id or f"local-socket:{self.socket_path}",
            store_path=store_path,
            max_builds=max_builds,
            max_transfers=max_transfers,
            supported_systems=supported_systems,
        )
        self.managed = managed
        self.nix_bin = nix_bin
        self.use_db = use_db
        self.daemon_proc: asyncio.subprocess.Process | None = None
        self.daemon_ready: asyncio.Event | None = None
        self.extra_env = extra_env or {}
        self.extra_args = extra_args or []

    @property
    def db_enabled(self) -> bool:
        return self.use_db

    async def ensure_daemon(self) -> None:
        """Spawn a managed daemon if needed (first call only).

        Uses an Event to coordinate concurrent callers — only the first
        spawns the daemon; others wait for it to be ready.
        """
        if not self.managed:
            return
        if self.daemon_proc is not None:
            # Daemon already spawned — wait for it to be ready
            if self.daemon_ready is not None:
                await self.daemon_ready.wait()
            return

        self.daemon_ready = asyncio.Event()

        path = self.store_path or Path("/")
        socket_dir = self.socket_path.parent
        os.makedirs(socket_dir, exist_ok=True)

        cmd = [
            self.nix_bin,
            "daemon",
            "--store",
            str(path),
            "--option",
            "build-dir",
            str(path / "tmp" / "nix-builds"),
        ]
        cmd.extend(self.extra_args)

        log.info(
            "spawning_managed_daemon",
            nix_bin=self.nix_bin,
            store_path=str(path),
            socket_path=str(self.socket_path),
            cmd=shlex.join(cmd),
        )
        env = os.environ.copy()
        env.update(self.extra_env)
        env["NIX_DAEMON_SOCKET_PATH"] = str(self.socket_path)

        self.daemon_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Wait for socket file to appear
        for _ in range(100):
            if self.socket_path.exists():
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(
                f"Managed daemon did not create socket at {self.socket_path} "
                f"within 5s (pid={self.daemon_proc.pid})",
            )

        # Socket file exists but daemon may not be listening yet — probe
        for attempt in range(50):
            try:
                r, w = await asyncio.open_unix_connection(str(self.socket_path))
                w.close()
                await w.wait_closed()
                log.info("daemon_socket_ready", socket_path=str(self.socket_path))
                self.daemon_ready.set()
                return
            except (ConnectionRefusedError, ConnectionResetError):
                await asyncio.sleep(0.2)

        raise RuntimeError(
            f"Managed daemon socket exists but not accepting connections "
            f"at {self.socket_path} within 5s (pid={self.daemon_proc.pid})",
        )

    async def create_conn(self) -> Connection:
        await self.ensure_daemon()
        conn_id = f"{self.id}-{self.conn_counter}"
        log.debug(
            "connecting_daemon_socket",
            socket_path=str(self.socket_path),
            conn_id=conn_id,
        )
        r, w = await asyncio.open_unix_connection(str(self.socket_path))
        conn = Connection(
            UnixNixReader(r), UnixNixWriter(w), conn_id, store_path=self.store_path
        )
        await conn.connect()
        return conn

    async def close(self) -> None:
        """Close stores and terminate managed daemon if any."""
        await super().close()
        if self.managed and self.daemon_proc is not None:
            self.daemon_proc.terminate()
            try:
                await asyncio.wait_for(self.daemon_proc.wait(), timeout=5.0)
            except TimeoutError:
                self.daemon_proc.kill()
            self.daemon_proc = None
            # Small delay to let OS clean up the socket file
            await asyncio.sleep(0.1)


DAEMON_SOCKET_PATH = Path("/nix/var/nix/daemon-socket/socket")


class SSHSocketStore(_SSHStoreMixin, Store):
    """Persistent SSH connection, tunnels to remote Unix socket."""

    def __init__(
        self,
        host: str,
        id: str | None = None,
        port: int = 22,
        username: str | None = None,
        socket_path: Path = DAEMON_SOCKET_PATH,
        max_builds: int = 2,
        max_transfers: int = 4,
        supported_systems: list[str] | None = None,
        monitor: bool = True,
        client_keys: list[str | Path | asyncssh.SSHKey] | None = None,
    ) -> None:
        super().__init__(
            id=id or f"ssh-socket:{username or ''}@{host}:{port}",
            max_builds=max_builds,
            max_transfers=max_transfers,
            supported_systems=supported_systems,
        )
        self.host = host
        self.port = port
        self.username = username
        self.socket_path = socket_path
        self.init_ssh_state(monitor=monitor, client_keys=client_keys)

    async def create_conn(self) -> Connection:
        try:
            ssh_conn = await self.ensure_ssh()
        except Exception:
            raise
        conn_id = f"{self.id}-{self.conn_counter}"
        log.debug(
            "tunneling_to_socket",
            socket_path=str(self.socket_path),
            conn_id=conn_id,
        )
        try:
            r, w = await ssh_conn.open_unix_connection(str(self.socket_path))
        except Exception:
            self.invalidate_ssh()
            raise
        conn = Connection(SSHNixReader(r), SSHNixWriter(w), conn_id)
        await conn.connect()
        return conn

    async def close(self) -> None:
        """Close stores and SSH connection."""
        await super().close()
        await self.close_ssh()


def get_current_system() -> str:
    """Return the current nix system string (e.g. x86_64-linux)."""
    try:
        return (
            subprocess.check_output(
                ["nix", "eval", "--raw", "--impure", "--expr", "builtins.currentSystem"]
            )
            .decode()
            .strip()
        )
    except Exception:
        # Fallback for systems where nix might not be in PATH
        # but is at a standard location.
        try:
            return (
                subprocess.check_output(
                    [
                        "/nix/var/nix/profiles/default/bin/nix",
                        "eval",
                        "--raw",
                        "--impure",
                        "--expr",
                        "builtins.currentSystem",
                    ]
                )
                .decode()
                .strip()
            )
        except Exception:
            machine = platform.machine()
            system = platform.system().lower()
            if system == "darwin":
                system = "darwin"
            return f"{machine}-{system}"
