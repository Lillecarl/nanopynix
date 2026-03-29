"""
Store ABC and concrete types for build machines.

A Store manages on-demand Connection connections to a single build machine,
handling connection pooling, concurrency limiting, and idle TTL cleanup.
Each Store type handles transport setup (subprocess, SSH channel, socket)
and constructs Connection instances with the resulting reader/writer pair.

Store types:
- LocalSubprocessStore: spawns local nix-daemon --stdio
- LocalSocketStore: connects to local nix-daemon Unix socket
- SSHSubprocessStore: persistent SSH, nix-daemon --stdio channels
- SSHSocketStore: persistent SSH, Unix socket tunnels
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import asyncssh

from . import stderr, wire
from .connection import ClientConn, Connection
from .local_store_db import LocalStoreDB
from .operations.base import EmptyResponse, PathInfo, SingleStringRequest
from .operations.builds import BuildDerivationRequest, BuildDerivationResponse
from .operations.maintenance import (
    CollectGarbageRequest,
    CollectGarbageResponse,
)
from .operations.queries import (
    IsValidPathRequest,
    NarFromPathRequest,
    QueryAllValidPathsRequest,
    QueryPathInfoRequest,
    QueryValidPathsRequest,
)
from .operations.store_mutations import (
    AddMultipleToStoreRequest,
    AddToStoreNarRequest,
    AddToStoreRequest,
    AddToStoreResponse,
)
from .protocol import Op
from .psi import MemInfo, PsiSnapshot, parse_meminfo, parse_psi_output
from .wire import (
    _CHUNK_SIZE,
    NixReader,
    NixWriter,
    SSHNixReader,
    SSHNixWriter,
    UnixNixReader,
    UnixNixWriter,
)

log: logging.Logger = logging.getLogger(__name__)
pool_log: logging.Logger = logging.getLogger(f"{__name__}.pool")

_DEFAULT_IDLE_TTL: float = 10.0
_CB_THRESHOLD: int = 3  # failures before cooldown
_CB_MAX_COOLDOWN: float = 300.0  # 5 min max


class Store(ABC):
    """A build store with on-demand connection pooling.

    Subclasses implement _create_conn() to set up transport and
    return a connected Connection. The base class handles pooling,
    concurrency limiting, and idle TTL cleanup.

    Idle connections are automatically closed after idle_ttl seconds.
    """

    def __init__(
        self,
        id: str,
        store_path: str | None = None,
        max_builds: int = 2,
        max_transfers: int = 4,
        idle_ttl: float = _DEFAULT_IDLE_TTL,
        supported_systems: list[str] | None = None,
    ) -> None:
        self.id = id
        self.store_path = store_path
        self.version: int = wire.PROTOCOL_VERSION
        self._max_builds = max_builds
        self._max_transfers = max_transfers
        self._idle_ttl = idle_ttl
        self._build_semaphore = asyncio.Semaphore(max_builds)
        self._transfer_semaphore = asyncio.Semaphore(max_transfers)
        self._idle: list[tuple[Connection, float]] = []
        self._all: list[Connection] = []
        self._conn_counter: int = 0
        self._sweep_task: asyncio.Task[None] | None = None
        self._supported_systems = supported_systems
        self._known_paths: set[str] = set()
        self._consecutive_failures: int = 0
        self._cooldown_until: float = 0.0
        self.db: LocalStoreDB | None = None

    @property
    def supported_systems(self) -> list[str]:
        """Systems this store can build for. Empty means all systems."""
        return self._supported_systems or []

    def supports_system(self, system: str) -> bool:
        """Check if this store supports the given system."""
        if not self._supported_systems:
            return True  # No restriction = supports all
        return system in self._supported_systems

    # ── Circuit breaker ──────────────────────────────────────────────

    @property
    def is_healthy(self) -> bool:
        """False while in cooldown. Becomes True when cooldown expires (half-open)."""
        return time.monotonic() >= self._cooldown_until

    def record_success(self) -> None:
        """Reset circuit breaker on successful operation."""
        if self._consecutive_failures > 0:
            log.info(
                "Store %s: recovered (was %d consecutive failures)",
                self.id,
                self._consecutive_failures,
            )
        self._consecutive_failures = 0
        self._cooldown_until = 0.0

    def record_failure(self) -> None:
        """Record a failure. After threshold, enter cooldown."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= _CB_THRESHOLD:
            cooldown = min(
                30 * 2 ** (self._consecutive_failures - _CB_THRESHOLD),
                _CB_MAX_COOLDOWN,
            )
            self._cooldown_until = time.monotonic() + cooldown
            log.warning(
                "Store %s: %d consecutive failures, cooling down %.0fs",
                self.id,
                self._consecutive_failures,
                cooldown,
            )

    # ── Known paths tracking ────────────────────────────────────────

    @property
    def known_paths(self) -> set[str]:
        return self._known_paths

    def has_path(self, path: str) -> bool:
        return path in self._known_paths

    def has_all_paths(self, paths: set[str]) -> bool:
        return paths.issubset(self._known_paths)

    def count_common_paths(self, paths: set[str]) -> int:
        return len(paths & self._known_paths)

    def add_known_path(self, path: str) -> None:
        self._known_paths.add(path)
        if self.db is not None:
            self.db.mark_path(path)

    def add_known_paths(self, paths: set[str]) -> None:
        self._known_paths.update(paths)
        if self.db is not None:
            self.db.mark_paths(paths)

    async def query_path_info(self, path: str) -> PathInfo | None:
        """Get PathInfo for a store path. DB first, daemon fallback."""
        if self.db is not None:
            result = await self.db.query_path_info(path)
            if result is not None:
                if result.valid:
                    return result.info
                else:
                    return None

        try:
            async with self.transfer_conn() as conn:
                resp = await conn.call(QueryPathInfoRequest(path=path))
                if resp.valid and resp.info is not None:
                    resp.info.path = path
                    return resp.info
                return None
        except Exception:
            log.debug(
                "query_path_info failed for %s on %s", path, self.id, exc_info=True
            )
            return None

    async def query_path_infos(self, paths: set[str]) -> dict[str, PathInfo]:
        """Batch PathInfo for multiple paths. DB fast path, daemon fallback."""
        if not paths:
            return {}

        if self.db is not None:
            result = await self.db.query_path_infos(paths)
            if result is not None:
                return result

        # Slow path: sequential query_path_info
        infos: dict[str, PathInfo] = {}
        for path in paths:
            info = await self.query_path_info(path)
            if info is not None:
                infos[path] = info
        return infos

    async def is_valid_path(self, path: str) -> bool:
        """Check if a path is valid on this store."""
        if self.has_path(path):
            return True
        try:
            async with self.transfer_conn() as conn:
                resp = await conn.call(IsValidPathRequest(path=path))
                return resp.valid
        except Exception:
            log.debug("is_valid_path failed for %s on %s", path, self.id)
            return False

    async def query_valid_paths(
        self,
        paths: set[str],
        substitute: bool = False,
    ) -> set[str]:
        """Query which paths are valid on this store."""
        async with self.transfer_conn() as conn:
            resp = await conn.call(
                QueryValidPathsRequest(
                    paths=paths,
                    substitute=1 if substitute else 0,
                )
            )
            return resp.paths

    async def query_all_valid_paths(self) -> set[str]:
        """Query all valid paths on this store."""
        async with self.transfer_conn() as conn:
            resp = await conn.call(QueryAllValidPathsRequest())
            return resp.paths

    async def stream_paths_store_to_store(
        self,
        src: Store,
        paths_with_info: list[tuple[str, PathInfo]],
    ) -> None:
        """Copy multiple paths from src store to this store via streaming."""
        if not paths_with_info:
            return

        async with (
            self.transfer_conn() as dst_conn,
            src.transfer_conn() as src_conn,
        ):
            try:
                dst_conn.w.write_uint64(Op.AddMultipleToStore)
                req = AddMultipleToStoreRequest(
                    repair=0,
                    dont_check_sigs=1,
                )
                await req.to_writer(dst_conn.w, dst_conn.version)

                fw = dst_conn.w.framed()
                fw.write_uint64(len(paths_with_info))

                for path, info in paths_with_info:
                    await info.to_writer_keyed(fw)

                    src_conn.w.write_uint64(Op.NarFromPath)
                    await SingleStringRequest(
                        path=path,
                    ).to_writer(src_conn.w, src_conn.version)
                    await src_conn.w.drain()
                    await stderr.drain(src_conn.r)

                    await wire.pipe_raw_to_framed_writer(
                        src_conn.r,
                        fw,
                        info.nar_size,
                    )

                await fw.finalize()

                await stderr.drain(dst_conn.r)
                await EmptyResponse.from_reader(dst_conn.r, dst_conn.version)
            except Exception:
                dst_conn.dirty = True
                src_conn.dirty = True
                raise

    async def add_to_store_nar_streaming(self, src: NixReader) -> str:
        """Stream AddToStoreNar from src to this store."""
        async with self.transfer_conn() as conn:
            try:
                path = await AddToStoreNarRequest.forward(src, conn.w)
                await conn.w.drain()
                await stderr.drain(conn.r)
                await EmptyResponse.from_reader(conn.r, conn.version)
                return path
            except Exception:
                conn.dirty = True
                raise

    async def add_to_store_streaming(self, src: NixReader) -> AddToStoreResponse:
        """Stream AddToStore from src to this store."""
        async with self.transfer_conn() as conn:
            try:
                await AddToStoreRequest.forward(src, conn.w)
                await conn.w.drain()
                await stderr.drain(conn.r)
                return await AddToStoreResponse.from_reader(conn.r, conn.version)
            except Exception:
                conn.dirty = True
                raise

    async def add_multiple_to_store_streaming(self, src: NixReader) -> list[str]:
        """Stream AddMultipleToStore from src to this store."""
        async with self.transfer_conn() as conn:
            try:
                paths = await AddMultipleToStoreRequest.forward(src, conn.w)
                await conn.w.drain()
                await stderr.drain(conn.r)
                await EmptyResponse.from_reader(conn.r, conn.version)
                return paths
            except Exception:
                conn.dirty = True
                raise

    async def buffer_nar_from_path(self, path: str, nar_size: int = 0) -> bytes:
        """Read NAR into memory."""
        async with self.transfer_conn() as conn:
            try:
                if nar_size > 0:
                    conn.w.write_uint64(Op.NarFromPath)
                    await SingleStringRequest(path=path).to_writer(conn.w, conn.version)
                    await conn.w.drain()
                    await stderr.drain(conn.r)
                    return await conn.r.readexactly(nar_size)
                else:
                    resp = await conn.call(NarFromPathRequest(path=path))
                    return resp.nar_data
            except Exception:
                conn.dirty = True
                raise

    async def stream_nar_from_path(
        self,
        path: str,
        dst: NixWriter,
        nar_size: int = 0,
        chunk_size: int = _CHUNK_SIZE,
    ) -> None:
        """Stream NAR to a NixWriter."""
        # If NAR size isn't specified we try to fetch it first
        if nar_size == 0:
            path_info = await self.query_path_info(path)
            if path_info is not None:
                nar_size = path_info.nar_size

        async with self.transfer_conn() as conn:
            try:
                conn.w.write_uint64(Op.NarFromPath)
                await SingleStringRequest(path=path).to_writer(conn.w, conn.version)
                await conn.w.drain()
                await stderr.drain(conn.r)
                if nar_size > 0:
                    remaining = nar_size
                    while remaining > 0:
                        to_read = min(remaining, chunk_size)
                        chunk = await conn.r.readexactly(to_read)
                        dst.write(chunk)
                        remaining -= to_read
                else:
                    await wire.stream_parse_nar(conn.r, dst)
            except Exception:
                conn.dirty = True
                raise

    async def nar_from_path_chunked(
        self,
        path: str,
        nar_size: int,
        write_chunk,
        chunk_size: int = _CHUNK_SIZE,
    ) -> None:
        """Stream NAR to an async callback in fixed-size chunks."""
        async with self.transfer_conn() as conn:
            try:
                conn.w.write_uint64(Op.NarFromPath)
                await SingleStringRequest(path=path).to_writer(conn.w, conn.version)
                await conn.w.drain()
                await stderr.drain(conn.r)
                remaining = nar_size
                while remaining > 0:
                    to_read = min(remaining, chunk_size)
                    chunk = await conn.r.readexactly(to_read)
                    await write_chunk(chunk)
                    remaining -= to_read
            except Exception:
                conn.dirty = True
                raise

    async def pipe_nar_from(
        self,
        src: Store,
        path: str,
        info: PathInfo,
    ) -> None:
        """Stream NAR from src store to this store."""
        async with self.transfer_conn() as dst_conn, src.transfer_conn() as src_conn:
            try:
                src_conn.w.write_uint64(Op.NarFromPath)
                await SingleStringRequest(
                    path=path,
                ).to_writer(src_conn.w, src_conn.version)
                await src_conn.w.drain()
                await stderr.drain(src_conn.r)

                dst_conn.w.write_uint64(Op.AddToStoreNar)
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

                await stderr.drain(dst_conn.r)
                await EmptyResponse.from_reader(dst_conn.r, dst_conn.version)
            except Exception:
                dst_conn.dirty = True
                src_conn.dirty = True
                raise

    async def build_derivation(
        self,
        request: BuildDerivationRequest,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> BuildDerivationResponse:
        """Execute a BuildDerivation on this store."""
        async with self.build_conn() as conn:
            return await conn.call(
                request,
                client=client,
                suppress_last=suppress_last,
            )

    async def collect_garbage(self, paths: set[str]) -> CollectGarbageResponse:
        """Delete specific paths via CollectGarbage (action=3)."""
        async with self.transfer_conn() as conn:
            resp = await conn.call(
                CollectGarbageRequest(
                    action=3,  # DeleteSpecific
                    paths_to_delete=paths,
                    ignore_liveness=0,
                    max_freed=0,
                )
            )
            self._known_paths -= resp.paths_deleted
            return resp

    async def sync_paths(self) -> None:
        """Query the daemon for all valid paths. Called once at startup.

        Falls back to empty set if the store doesn't support QueryAllValidPaths
        (e.g. nixbuild.net). Locality ranking just won't apply.
        """
        try:
            async with self.transfer_conn() as conn:
                resp = await conn.call(QueryAllValidPathsRequest())
                self._known_paths = resp.paths
            log.info("Store %s: %d known paths", self.id, len(self._known_paths))
        except Exception:
            log.warning(
                "Store %s: sync_paths failed, starting with empty known paths", self.id
            )
            self._known_paths = set()

    @property
    def available_transfer_slots(self) -> int:
        """Number of free transfer slots."""
        return self._transfer_semaphore._value

    @abstractmethod
    async def _create_conn(self) -> Connection:
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
        conns = await asyncio.gather(*[self._create_conn() for _ in range(n)])
        now = time.monotonic()
        for conn in conns:
            self._all.append(conn)
            self._idle.append((conn, now))
        self._start_sweep()
        log.info("Store %s: warmed pool with %d connections", self.id, n)

    @property
    def max_builds(self) -> int:
        return self._max_builds

    @property
    def available_slots(self) -> int:
        """Number of free build slots."""
        return self._build_semaphore._value

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
        return self._max_builds - self._build_semaphore._value

    @property
    def is_lix(self) -> bool:
        """True if this store is Lix (protocol version 1.35)."""
        return self.version == wire.proto(1, 35)

    def _start_sweep(self) -> None:
        """Start the idle sweep task if not already running."""
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(self._sweep_idle())

    async def _sweep_idle(self) -> None:
        """Periodically close idle connections that have expired."""
        while self._idle:
            await asyncio.sleep(self._idle_ttl / 2)
            now = time.monotonic()
            still_idle: list[tuple[Connection, float]] = []
            for conn, returned_at in self._idle:
                if now - returned_at >= self._idle_ttl:
                    pool_log.debug(
                        "Store %s: closing expired idle %s",
                        self.id,
                        conn.id,
                    )
                    self._all.remove(conn)
                    try:
                        await conn.close()
                    except Exception:
                        pass
                else:
                    still_idle.append((conn, returned_at))
            self._idle = still_idle

    @staticmethod
    async def _reader_is_dirty(conn: Connection) -> bool:
        """Check if the reader has unread buffered data (protocol desync)."""
        return await conn.r.is_dirty()

    async def _get_or_create_conn(self) -> Connection:
        """Pop an idle connection or create a new one."""
        now = time.monotonic()
        while self._idle:
            candidate, returned_at = self._idle.pop()
            if now - returned_at >= self._idle_ttl:
                pool_log.debug(
                    "Store %s: discarding expired %s",
                    self.id,
                    candidate.id,
                )
                self._all.remove(candidate)
                try:
                    await candidate.close()
                except Exception:
                    pass
                continue
            if candidate.dirty or await self._reader_is_dirty(candidate):
                log.warning(
                    "Store %s: discarding dirty connection %s (op log: %s)",
                    self.id,
                    candidate.id,
                    " -> ".join(candidate._op_log[-10:]) or "(empty)",
                )
                self._all.remove(candidate)
                try:
                    await candidate.close()
                except Exception:
                    pass
                continue
            pool_log.debug(
                "Store %s: reusing connection %s",
                self.id,
                candidate.id,
            )
            return candidate

        self._conn_counter += 1
        conn = await self._create_conn()
        self._all.append(conn)
        # Capture protocol version from first connection
        if self._conn_counter == 1:
            self.version = conn.version
            log.info(
                "Store %s: protocol version %s",
                self.id,
                wire.proto_str(self.version),
            )
        pool_log.debug(
            "Store %s: created connection %s [%s]",
            self.id,
            conn.id,
            self.pool_stats,
        )
        return conn

    @asynccontextmanager
    async def _acquire_conn(
        self,
        semaphore: asyncio.Semaphore,
    ) -> AsyncIterator[Connection]:
        """Acquire a connection from the shared pool.

        Blocks until the given semaphore allows entry, then pops an idle
        connection or creates a new one.
        """
        if semaphore._value == 0:
            kind = "build" if semaphore is self._build_semaphore else "transfer"
            limit = self._max_builds if kind == "build" else self._max_transfers
            pool_log.info(
                "Store %s: all %d %s slots in use, waiting",
                self.id,
                limit,
                kind,
            )
        await semaphore.acquire()
        conn: Connection | None = None
        try:
            conn = await self._get_or_create_conn()
            yield conn
        finally:
            if conn is not None:
                if conn.dirty:
                    log.warning(
                        "Store %s: discarding dirty connection %s (op log: %s)",
                        self.id,
                        conn.id,
                        " -> ".join(conn._op_log[-10:]) or "(empty)",
                    )
                    self._all.remove(conn)
                    try:
                        await conn.close()
                    except Exception:
                        pass
                else:
                    self._idle.append((conn, time.monotonic()))
                    self._start_sweep()
            semaphore.release()

    def build_conn(self) -> AbstractAsyncContextManager[Connection]:
        """Acquire a build connection (counts against max_builds)."""
        return self._acquire_conn(self._build_semaphore)

    def transfer_conn(self) -> AbstractAsyncContextManager[Connection]:
        """Acquire a transfer connection (counts against max_transfers).

        Transfer connections share the same pool as build connections
        but use a separate semaphore so transfers don't block builds.
        """
        return self._acquire_conn(self._transfer_semaphore)

    async def close(self) -> None:
        """Close all pooled connections and stop sweep task."""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            self._sweep_task = None
        for conn in self._all:
            try:
                await conn.close()
            except (ProcessLookupError, Exception):
                pass
        self._all.clear()
        self._idle.clear()

    @property
    def pool_stats(self) -> str:
        """Human-readable pool statistics."""
        build_in_use = self._max_builds - self._build_semaphore._value
        transfer_in_use = self._max_transfers - self._transfer_semaphore._value
        return (
            f"builds={build_in_use}/{self._max_builds} "
            f"transfers={transfer_in_use}/{self._max_transfers} "
            f"idle={len(self._idle)} total={len(self._all)}"
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(id={self.id!r}, "
            f"in_flight={self.in_flight}/{self._max_builds}, "
            f"idle={len(self._idle)}, "
            f"connections={len(self._all)})"
        )


class LocalSubprocessStore(Store):
    """Spawns local nix-daemon --stdio subprocesses."""

    def __init__(
        self,
        store_path: str,
        id: str | None = None,
        max_builds: int = 2,
        max_transfers: int = 4,
        supported_systems: list[str] | None = None,
        nix_bin: str = "nix",
    ) -> None:
        super().__init__(
            id=id or f"local:{store_path}",
            store_path=store_path,
            max_builds=max_builds,
            max_transfers=max_transfers,
            supported_systems=supported_systems,
        )
        self._nix_bin = nix_bin
        self._processes: list[asyncio.subprocess.Process] = []

    async def _create_conn(self) -> Connection:
        path = self.store_path or ""
        os.makedirs(path, exist_ok=True)

        conn_id = f"{self.id}-{self._conn_counter}"
        log.info(
            "Spawning %s daemon --store %s --stdio (%s)",
            self._nix_bin,
            path,
            conn_id,
        )
        proc = await asyncio.create_subprocess_exec(
            self._nix_bin,
            "daemon",
            "--store",
            path,
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        assert proc.stdin is not None
        self._processes.append(proc)

        conn = Connection(
            UnixNixReader(proc.stdout),
            UnixNixWriter(proc.stdin),
            conn_id,
            store_path=path,
        )
        await conn.connect()
        return conn

    async def close(self) -> None:
        """Close stores and terminate subprocesses."""
        await super().close()
        for proc in self._processes:
            try:
                proc.terminate()
            except ProcessLookupError:
                continue
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
        self._processes.clear()


class _SSHStoreMixin:
    """Shared SSH connection management with exponential backoff reconnection.

    Subclasses must set _host, _port, _username on __init__.
    """

    _host: str
    _port: int
    _username: str | None
    _conn: asyncssh.SSHClientConnection | None
    _backoff: float
    _max_backoff: float
    _last_failure: float
    id: str

    _INITIAL_BACKOFF: float = 1.0
    _MAX_BACKOFF: float = 60.0
    _PSI_INTERVAL: float = float(os.environ.get("PYNIXD_PSI_INTERVAL", "5"))

    def _init_ssh_state(self, *, monitor: bool = True) -> None:
        self._conn = None
        self._ssh_lock = asyncio.Lock()
        self._backoff = self._INITIAL_BACKOFF
        self._max_backoff = self._MAX_BACKOFF
        self._last_failure = 0.0
        self._monitor = monitor
        self._psi: PsiSnapshot | None = None
        self._meminfo: MemInfo | None = None
        self._psi_task: asyncio.Task[None] | None = None

    def _start_psi_polling(self) -> None:
        """Start PSI polling loop. Called after first successful SSH connect."""
        if not self._monitor:
            return
        if self._psi_task is None or self._psi_task.done():
            self._psi_task = asyncio.create_task(self._psi_poll_loop())

    _PSI_FILES: tuple[str, ...] = (
        "/proc/pressure/cpu",
        "/proc/pressure/memory",
        "/proc/pressure/io",
    )

    async def _psi_poll_loop(self) -> None:
        """Periodically read PSI and meminfo data over SFTP."""
        while True:
            try:
                conn = self._conn
                if conn is None:
                    await asyncio.sleep(self._PSI_INTERVAL)
                    continue
                async with conn.start_sftp_client() as sftp:
                    while True:
                        parts = []
                        for path in self._PSI_FILES:
                            async with sftp.open(path, "r") as f:
                                parts.append(await f.read())
                        self._psi = parse_psi_output("".join(parts))
                        try:
                            async with sftp.open("/proc/meminfo", "r") as f:
                                self._meminfo = parse_meminfo(await f.read())
                        except asyncssh.SFTPError:
                            pass  # meminfo optional
                        await asyncio.sleep(self._PSI_INTERVAL)
            except asyncio.CancelledError:
                return
            except (
                asyncssh.SFTPNoSuchFile,
                asyncssh.SFTPPermissionDenied,
                asyncssh.SFTPOpUnsupported,
            ) as e:
                # PSI not available on this host (macOS, old kernel, restricted perms)
                log.info(
                    "Store %s: PSI unavailable (%s), disabling polling", self.id, e
                )
                self._psi = None
                return
            except asyncssh.SFTPConnectionLost:
                # SFTP channel died, retry after SSH reconnects
                log.debug("Store %s: PSI SFTP connection lost, will retry", self.id)
                self._psi = None
                await asyncio.sleep(self._PSI_INTERVAL)
            except asyncssh.SFTPError as e:
                # Any other SFTP error — probably not recoverable
                log.info("Store %s: PSI SFTP error (%s), disabling polling", self.id, e)
                self._psi = None
                return
            except (asyncssh.Error, OSError) as e:
                # SSH connection-level error — retry, SSH reconnect may fix it
                log.debug("Store %s: PSI SSH error (%s), will retry", self.id, e)
                self._psi = None
                await asyncio.sleep(self._PSI_INTERVAL)

    def _stop_psi_polling(self) -> None:
        """Cancel the PSI polling task."""
        if self._psi_task is not None:
            self._psi_task.cancel()
            self._psi_task = None

    @property
    def pressure(self) -> float | None:
        """System pressure score (0-100), or None if unavailable."""
        if self._psi is None:
            return None
        # Stale check: 3x interval
        if time.monotonic() - self._psi.timestamp > self._PSI_INTERVAL * 3:
            return None
        return self._psi.pressure_score()

    @property
    def meminfo(self) -> MemInfo | None:
        """System memory info, or None if unavailable."""
        return self._meminfo

    async def _ensure_ssh(self) -> asyncssh.SSHClientConnection:
        if self._conn is not None:
            return self._conn

        async with self._ssh_lock:
            # Re-check after acquiring lock (another task may have connected)
            if self._conn is not None:
                return self._conn

            # Respect backoff from previous failure
            now = time.monotonic()
            wait = self._last_failure + self._backoff - now
            if self._last_failure > 0 and wait > 0:
                log.info(
                    "Store %s: SSH backoff %.1fs before reconnecting",
                    self.id,
                    wait,
                )
                await asyncio.sleep(wait)

            try:
                log.info(
                    "SSH connecting to %s@%s:%d",
                    self._username or "",
                    self._host,
                    self._port,
                )
                self._conn = await asyncssh.connect(
                    self._host,
                    port=self._port,
                    username=self._username,
                    known_hosts=None,
                )
                # Reset backoff on success
                self._backoff = self._INITIAL_BACKOFF
                self._last_failure = 0.0
                self.record_success()  # type: ignore[attr-defined]
                self._start_psi_polling()
                return self._conn
            except Exception:
                self._last_failure = time.monotonic()
                self._backoff = min(self._backoff * 2, self._max_backoff)
                self.record_failure()  # type: ignore[attr-defined]
                log.warning(
                    "Store %s: SSH connect failed, next retry in %.1fs",
                    self.id,
                    self._backoff,
                )
                raise

    def _invalidate_ssh(self) -> None:
        """Mark SSH connection as dead so next _ensure_ssh reconnects."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    async def _close_ssh(self) -> None:
        self._stop_psi_polling()
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class SSHSubprocessStore(_SSHStoreMixin, Store):
    """Persistent SSH connection, spawns nix-daemon --stdio channels.

    If store_path is set, runs ``nix daemon --store <path> --stdio``.
    Otherwise runs ``nix-daemon --stdio`` (default store, nixbuild.net compat).
    """

    def __init__(
        self,
        host: str,
        id: str | None = None,
        port: int = 22,
        username: str | None = None,
        store_path: str | None = None,
        max_builds: int = 2,
        max_transfers: int = 4,
        supported_systems: list[str] | None = None,
        monitor: bool = True,
    ) -> None:
        super().__init__(
            id=id or f"ssh:{username or ''}@{host}:{port}",
            store_path=store_path,
            max_builds=max_builds,
            max_transfers=max_transfers,
            supported_systems=supported_systems,
        )
        self._host = host
        self._port = port
        self._username = username
        self._init_ssh_state(monitor=monitor)
        self._ssh_processes: list[asyncssh.SSHClientProcess] = []

    async def _create_conn(self) -> Connection:
        try:
            ssh_conn = await self._ensure_ssh()
        except Exception:
            raise
        conn_id = f"{self.id}-{self._conn_counter}"
        if self.store_path:
            cmd = f"nix daemon --store {self.store_path} --stdio"
        else:
            cmd = "nix-daemon --stdio"
        log.debug(
            "Spawning remote %s (%s)",
            cmd,
            conn_id,
        )
        try:
            proc = await ssh_conn.create_process(cmd, encoding=None)
        except Exception:
            self._invalidate_ssh()
            raise
        self._ssh_processes.append(proc)
        proc.channel.set_write_buffer_limits(
            high=wire._SSH_WINDOW_SIZE, low=wire._SSH_WINDOW_SIZE // 4
        )

        conn = Connection(SSHNixReader(proc.stdout), SSHNixWriter(proc.stdin), conn_id)
        await conn.connect()
        return conn

    async def close(self) -> None:
        """Close stores, SSH processes, and SSH connection."""
        await super().close()
        for proc in self._ssh_processes:
            try:
                proc.terminate()
            except Exception:
                pass
            proc.close()
        self._ssh_processes.clear()
        await self._close_ssh()


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
        store_path: str = "/",
        max_builds: int = 1,
        max_transfers: int = 4,
        supported_systems: list[str] | None = None,
        nix_bin: str = "nix",
    ) -> None:
        managed = store_path != "/"
        if managed:
            socket_path = os.path.join(
                store_path,
                "var",
                "nix",
                "daemon-socket",
                "socket",
            )
        else:
            socket_path = DAEMON_SOCKET_PATH

        super().__init__(
            id=id or f"local-socket:{socket_path}",
            store_path=store_path,
            max_builds=max_builds,
            max_transfers=max_transfers,
            supported_systems=supported_systems,
        )
        self._socket_path = socket_path
        self._managed = managed
        self._nix_bin = nix_bin
        self._daemon_proc: asyncio.subprocess.Process | None = None
        self._daemon_ready: asyncio.Event | None = None

    async def _ensure_daemon(self) -> None:
        """Spawn a managed daemon if needed (first call only).

        Uses an Event to coordinate concurrent callers — only the first
        spawns the daemon; others wait for it to be ready.
        """
        if not self._managed:
            return
        if self._daemon_proc is not None:
            # Daemon already spawned — wait for it to be ready
            if self._daemon_ready is not None:
                await self._daemon_ready.wait()
            return

        self._daemon_ready = asyncio.Event()

        path = self.store_path or ""
        socket_dir = os.path.dirname(self._socket_path)
        os.makedirs(socket_dir, exist_ok=True)

        log.info(
            "Spawning managed daemon: %s daemon --store %s (socket %s)",
            self._nix_bin,
            path,
            self._socket_path,
        )
        env = os.environ.copy()
        env["NIX_DAEMON_SOCKET_PATH"] = self._socket_path
        self._daemon_proc = await asyncio.create_subprocess_exec(
            self._nix_bin,
            "daemon",
            "--store",
            path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )

        # Wait for socket file to appear
        for _ in range(100):
            if os.path.exists(self._socket_path):
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError(
                f"Managed daemon did not create socket at {self._socket_path} "
                f"within 5s (pid={self._daemon_proc.pid})",
            )

        # Socket file exists but daemon may not be listening yet — probe
        for attempt in range(50):
            try:
                r, w = await asyncio.open_unix_connection(self._socket_path)
                w.close()
                await w.wait_closed()
                log.info("Managed daemon socket ready: %s", self._socket_path)
                self._daemon_ready.set()
                return
            except (ConnectionRefusedError, ConnectionResetError):
                await asyncio.sleep(0.1)

        raise RuntimeError(
            f"Managed daemon socket exists but not accepting connections "
            f"at {self._socket_path} within 5s (pid={self._daemon_proc.pid})",
        )

    async def _create_conn(self) -> Connection:
        await self._ensure_daemon()
        conn_id = f"{self.id}-{self._conn_counter}"
        log.debug("Connecting to daemon socket %s (%s)", self._socket_path, conn_id)
        r, w = await asyncio.open_unix_connection(self._socket_path)
        conn = Connection(
            UnixNixReader(r), UnixNixWriter(w), conn_id, store_path=self.store_path
        )
        await conn.connect()
        return conn

    async def close(self) -> None:
        """Close stores and terminate managed daemon if any."""
        await super().close()
        if self._daemon_proc is not None:
            self._daemon_proc.terminate()
            try:
                await asyncio.wait_for(self._daemon_proc.wait(), timeout=5.0)
            except TimeoutError:
                self._daemon_proc.kill()
            self._daemon_proc = None


DAEMON_SOCKET_PATH = "/nix/var/nix/daemon-socket/socket"


class SSHSocketStore(_SSHStoreMixin, Store):
    """Persistent SSH connection, tunnels to remote Unix socket."""

    def __init__(
        self,
        host: str,
        id: str | None = None,
        port: int = 22,
        username: str | None = None,
        socket_path: str = DAEMON_SOCKET_PATH,
        max_builds: int = 2,
        max_transfers: int = 4,
        supported_systems: list[str] | None = None,
        monitor: bool = True,
    ) -> None:
        super().__init__(
            id=id or f"ssh-socket:{username or ''}@{host}:{port}",
            max_builds=max_builds,
            max_transfers=max_transfers,
            supported_systems=supported_systems,
        )
        self._host = host
        self._port = port
        self._username = username
        self._socket_path = socket_path
        self._init_ssh_state(monitor=monitor)

    async def _create_conn(self) -> Connection:
        try:
            ssh_conn = await self._ensure_ssh()
        except Exception:
            raise
        conn_id = f"{self.id}-{self._conn_counter}"
        log.debug(
            "Tunneling to %s (%s)",
            self._socket_path,
            conn_id,
        )
        try:
            r, w = await ssh_conn.open_unix_connection(self._socket_path)
        except Exception:
            self._invalidate_ssh()
            raise
        conn = Connection(SSHNixReader(r), SSHNixWriter(w), conn_id)
        await conn.connect()
        return conn

    async def close(self) -> None:
        """Close stores and SSH connection."""
        await super().close()
        await self._close_ssh()
