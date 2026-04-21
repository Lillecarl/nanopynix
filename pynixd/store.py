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
from enum import IntEnum
from pathlib import Path

import asyncssh
import structlog
from cachetools import TTLCache
from environs import env

from . import wire
from .connection import ClientConn, Connection
from .local_store_db import LocalStoreDB
from .operations.add_multiple_to_store import AddMultipleToStoreRequest
from .operations.add_to_store_nar import AddToStoreNarRequest
from .operations.base import (
    OpRequest,
    Resp,
    ValidPathInfo,
)
from .operations.nar_from_path import NarFromPathRequest
from .operations.query_closure_with_info import QueryClosureWithInfoRequest
from .psi import (
    CgroupCpuStat,
    CpuUtil,
    MemInfo,
    PsiSnapshot,
    compute_cpu_util,
    count_cpus_from_proc_stat,
    parse_cpu_max,
    parse_cpu_stat,
    parse_psi_output,
)
from .signing import SecretKey
from .path_tracker import PathTrackerInstance
from .store_path import StorePath
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


class ProbeState(IntEnum):
    NOT_PROBED = 0
    PROBING = 1
    PROBED = 2


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
        max_transfers: int = 16,
        idle_ttl: float = _DEFAULT_IDLE_TTL,
        feature_matrix: dict[str, set[str]] | None = None,
        probe: bool = True,
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
        self._feature_matrix: dict[str, set[str]] | None = feature_matrix
        self._probe = probe
        self.tracker: PathTrackerInstance = PathTrackerInstance(store_id=id)
        self.path_info_cache: TTLCache[StorePath, ValidPathInfo] = TTLCache(
            maxsize=10000, ttl=300
        )
        self.consecutive_failures: int = 0
        self.cooldown_until: float = 0.0
        self.db: LocalStoreDB | None = None
        self.features: set[str] = set()
        self.probe_state: ProbeState = ProbeState.NOT_PROBED
        self._probe_event: asyncio.Event = asyncio.Event()
        self.signing_keys: dict[str, SecretKey] = {}
        self._holder_task: asyncio.Task | None = None
        self.draining: bool = False

    @property
    def db_enabled(self) -> bool:
        """True if this store should use a local SQLite DB for metadata."""
        return True

    @property
    def native_db(self) -> LocalStoreDB | None:
        """The local SQLite DB if it is the native database for this store root."""
        if self.db is not None and self.db.store_path == self.store_path:
            return self.db
        return None

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

    @property
    def feature_matrix(self) -> dict[str, set[str]]:
        """Per-system feature mapping: {system: {features}}."""
        if self._feature_matrix is not None:
            return self._feature_matrix
        return {}

    @feature_matrix.setter
    def feature_matrix(self, value: dict[str, set[str]]) -> None:
        self._feature_matrix = value

    @property
    def systems(self) -> set[str] | None:
        """Set of systems this store supports, derived from feature_matrix keys."""
        fm = self._feature_matrix
        if fm is not None:
            return set(fm.keys()) if fm else None
        return None

    @systems.setter
    def systems(self, value: set[str] | None) -> None:
        """Setting systems updates the feature_matrix keys, preserving features."""
        if self._feature_matrix is not None:
            if value is None:
                self._feature_matrix = {}
                return
            old = self._feature_matrix
            self._feature_matrix = {s: old.get(s, set()) for s in value}
        elif value is not None:
            self._feature_matrix = {s: set() for s in value}

    def supports_system(self, system: str) -> bool:
        """Check if this store supports the given system."""
        fm = self._feature_matrix
        if fm is None:
            return True
        return system in fm

    def supports_derivation(
        self, system: str, features: set[str] | None = None
    ) -> bool:
        """Check if this store can build a derivation requiring the given
        system and set of requiredSystemFeatures.

        Returns True only if the store supports the system AND that system
        has ALL the required features in the feature matrix.
        """
        fm = self._feature_matrix
        if fm is not None:
            sys_features = fm.get(system)
            if sys_features is None:
                return False
            if not features:
                return True
            return features.issubset(sys_features)
        if not features:
            return True
        return True

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
        return path in self.tracker.known_paths

    def has_all_paths(self, paths: set[StorePath]) -> bool:
        return paths.issubset(self.tracker.known_paths)

    def count_common_paths(self, paths: set[StorePath]) -> int:
        return len(paths & self.tracker.known_paths)

    async def call(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
        raise_on_error: bool = False,
        skip_probe: bool = False,
    ) -> Resp:
        """Send an operation to this store. Handles connection lifecycle.

        Args:
            request: The operation request object.
            client: Optional client connection for stderr forwarding.
            suppress_last: If True, consume but don't forward STDERR_LAST.
            raise_on_error: Whether to raise BackendError on daemon errors.
        """
        if not skip_probe:
            await self.probe()

        # Use build_conn for builds, transfer_conn for queries/mutations
        if request.is_build:
            pool = self.build_conn
        else:
            pool = self.transfer_conn

        async with pool() as conn:
            res = await conn.call(
                request,
                client=client,
                suppress_last=suppress_last,
                raise_on_error=raise_on_error,
            )
            # log.debug(
            #     "connection_releasing", pool=pool_name, op=request.__class__.__name__
            # )
            return res

    async def execute(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
        skip_probe: bool = False,
    ) -> Resp:
        """Execute an operation on this store.

        Delegates logic to the request object, which may use fast-paths
        (SQLite, memory), schedule builds or fallback to this store's 'call' method.
        """
        if not skip_probe:
            await self.probe()
        return await request.execute(
            self,
            client=client,
            suppress_last=suppress_last,
        )

    def add_path_info(self, info: ValidPathInfo) -> None:
        """Add ValidPathInfo to the cache."""
        self.path_info_cache[info.path] = info

    def add_path_infos(self, infos: Iterable[ValidPathInfo]) -> None:
        """Add multiple ValidPathInfos to the cache."""
        for info in infos:
            self.path_info_cache[info.path] = info

    def get_path_info(self, path: StorePath) -> ValidPathInfo | None:
        """Get ValidPathInfo from cache if available."""
        return self.path_info_cache.get(path)

    async def stream_paths_to(
        self,
        dst: Store,
        paths: Iterable[StorePath],
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Copy paths from this store to dst via streaming, querying closure first.

        Bypasses the normal handle() path, so we update dst knowledge manually.
        Only transfers paths that dst doesn't already have.
        """
        paths_set: set[StorePath] = {StorePath(p) for p in paths}
        if not paths_set:
            return

        # 1. Get closure from source
        closure_resp = await self.execute(
            QueryClosureWithInfoRequest(paths=paths_set),
            client=None,
        )
        if not closure_resp.infos:
            return

        # 2. Filter out paths already in destination
        to_transfer: list[ValidPathInfo] = [
            info
            for info in closure_resp.infos
            if info.path not in dst.tracker.known_paths
        ]
        if not to_transfer:
            return

        # 3. Stream the missing paths
        async with self.transfer_conn() as src_conn, dst.transfer_conn() as dst_conn:
            dst_conn.op_log.append("AddMultipleToStore (stream_paths_to)")
            req = AddMultipleToStoreRequest(
                repair=0,
                dont_check_sigs=1,
            )
            await req.to_writer(dst_conn.w, dst_conn.version)
            await dst_conn.w.drain()

            fw = dst_conn.w.framed()
            fw.write_uint64(len(to_transfer))

            for info in to_transfer:
                if cancel_event and cancel_event.is_set():
                    log.info("stream_paths_transfer_cancelled")
                    break

                path = info.path
                dst_conn.op_log.append("AddToStoreNar (stream_paths_to)")

                # Use info.to_bytes() to send metadata as a single frame
                fw.write(info.to_bytes())

                # Request NAR from source
                await NarFromPathRequest(path=path).to_writer(
                    src_conn.w, src_conn.version
                )
                await src_conn.w.drain()

                # Source will send stderr logs followed by STDERR_LAST before NAR data
                await src_conn.r.drain_stderr()

                # Pipe raw NAR data from source into the destination's framed stream
                await wire.pipe_raw_to_framed_writer(
                    src_conn.r,
                    fw,
                    info.nar_size,
                )
                await dst_conn.w.drain()

            await fw.finalize()
            await dst_conn.w.drain()
            await req.response_type().from_reader(dst_conn.r, dst_conn.version)

        # 4. Update destination store's knowledge
        dst.add_path_infos(set(to_transfer))
        dst.tracker.add_known_paths({i.path for i in to_transfer})

    async def pipe_nar_from(
        self,
        src: Store,
        path: StorePath,
        info: ValidPathInfo,
    ) -> None:
        """Stream NAR from src store to this store."""
        async with self.transfer_conn() as dst_conn, src.transfer_conn() as src_conn:
            await NarFromPathRequest(path=path).to_writer(src_conn.w, src_conn.version)
            await src_conn.w.drain()

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

            await nar_request.response_type().from_reader(dst_conn.r, dst_conn.version)

    @property
    def available_transfer_slots(self) -> int:
        """Number of free transfer slots."""
        return self.transfer_semaphore._value

    @abstractmethod
    async def create_conn(self) -> Connection:
        """Create transport, construct Connection, and connect it."""
        ...

    async def probe(self) -> None:
        """Discover the daemon's protocol version, systems, and system features.

        Concurrent callers block on ``_probe_event`` while the first caller
        does the work.  The state transitions NOT_PROBED -> PROBING -> PROBED.

        When ``_probe`` is False, build-based probing is skipped — the
        feature_matrix supplied at construction is used as-is and the
        store is marked probed after warming the connection pool.
        """
        if self.probe_state == ProbeState.PROBED:
            return

        if self.probe_state == ProbeState.PROBING:
            await self._probe_event.wait()
            return

        self.probe_state = ProbeState.PROBING

        if not self._probe:
            await self.warm_pool(1)
            self.probe_state = ProbeState.PROBED
            self._probe_event.set()
            return

        from .operations.probe_systems import ProbeSystemsRequest
        from .system_features import PROBE_SYSTEMS, KNOWN_FEATURES

        existing_systems = (
            set(self._feature_matrix.keys()) if self._feature_matrix else set()
        )
        existing_features: set[str] = set()
        if self._feature_matrix:
            for feats in self._feature_matrix.values():
                existing_features.update(feats)

        candidate_systems = existing_systems or set(PROBE_SYSTEMS)
        candidate_features = existing_features or set(KNOWN_FEATURES)

        systems_resp = await ProbeSystemsRequest(
            systems=candidate_systems,
        ).execute(self)

        from .operations.probe_features import ProbeFeaturesRequest

        features_resp = await ProbeFeaturesRequest(
            systems=systems_resp.systems,
            system_features=candidate_features,
        ).execute(self)

        self._feature_matrix = features_resp.feature_matrix

        log.info(
            "store_probed",
            store_id=self.id,
            systems=sorted(self._feature_matrix.keys()) if self._feature_matrix else [],
            feature_matrix={
                k: sorted(v) for k, v in (self._feature_matrix or {}).items()
            },
        )

        self.probe_state = ProbeState.PROBED
        self._probe_event.set()

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
    def cpu_util(self) -> CpuUtil | None:
        """CPU utilization from cgroupv2, or None if unavailable."""
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
        self.version = conn.version
        self.nix_version = conn.nix_version
        self.features = conn.features
        pool_log.debug(
            "pool_created_connection",
            store_id=self.id,
            conn_id=conn.id,
            pool_stats=self.pool_stats,
            version=wire.proto_str(self.version),
            nix_version=self.nix_version,
            features=sorted(self.features),
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


class _SSHStoreMixin(Store):
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
        self.cpu_stat_prev: CgroupCpuStat | None = None
        self.cpu_stat_curr: CgroupCpuStat | None = None
        self.cpu_cores: float | None = None
        self.cpu_util_data: CpuUtil | None = None
        self.psi_task: asyncio.Task[None] | None = None

    def start_psi_polling(self) -> None:
        """Start PSI polling loop. Called after first successful SSH connect."""
        if not self.monitor_enabled:
            return
        if self.psi_task is None or self.psi_task.done():
            self.psi_task = asyncio.create_task(self.psi_poll_loop())

    PSI_FILES: tuple[str, ...] = (
        "/sys/fs/cgroup/cpu.pressure",
        "/sys/fs/cgroup/memory.pressure",
        "/sys/fs/cgroup/io.pressure",
    )

    async def psi_poll_loop(self) -> None:
        """Periodically read PSI, meminfo, and cpu stats over SFTP.

        All paths are cgroupv2 under /sys/fs/cgroup/. If cgroupv2 is not
        available, polling stops gracefully.
        """
        while True:
            try:
                conn = self.conn
                if conn is None:
                    await asyncio.sleep(self.PSI_INTERVAL)
                    continue
                async with conn.start_sftp_client() as sftp:
                    # Read cpu.max once — the quota rarely changes
                    # Fall back to /proc/stat for nproc if no cpu.max (root cgroup)
                    if self.cpu_cores is None:
                        try:
                            async with sftp.open("/sys/fs/cgroup/cpu.max", "r") as f:
                                self.cpu_cores = parse_cpu_max(await f.read())
                        except asyncssh.SFTPError:
                            pass
                        if self.cpu_cores is None:
                            try:
                                async with sftp.open("/proc/stat", "r") as f:
                                    self.cpu_cores = float(
                                        count_cpus_from_proc_stat(await f.read())
                                    )
                            except asyncssh.SFTPError:
                                pass

                    while True:
                        parts = []
                        for path in self.PSI_FILES:
                            async with sftp.open(path, "r") as f:
                                parts.append(await f.read())
                        self.psi_data = parse_psi_output("".join(parts))

                        try:
                            async with sftp.open("/sys/fs/cgroup/cpu.stat", "r") as f:
                                stat = parse_cpu_stat(await f.read())
                            self.cpu_stat_prev = self.cpu_stat_curr
                            self.cpu_stat_curr = stat
                            if (
                                self.cpu_stat_prev is not None
                                and self.cpu_stat_curr is not None
                            ):
                                self.cpu_util_data = compute_cpu_util(
                                    self.cpu_stat_prev,
                                    self.cpu_stat_curr,
                                    self.cpu_cores,
                                )
                        except asyncssh.SFTPError:
                            pass

                        try:
                            async with sftp.open(
                                "/sys/fs/cgroup/memory.current", "r"
                            ) as f:
                                mem_current = int((await f.read()).strip())
                            async with sftp.open("/sys/fs/cgroup/memory.max", "r") as f:
                                mem_max_raw = (await f.read()).strip()
                            mem_max = None if mem_max_raw == "max" else int(mem_max_raw)
                            try:
                                async with sftp.open(
                                    "/sys/fs/cgroup/swap.current", "r"
                                ) as f:
                                    swap_current = int((await f.read()).strip())
                            except asyncssh.SFTPError:
                                swap_current = 0
                            try:
                                async with sftp.open(
                                    "/sys/fs/cgroup/swap.max", "r"
                                ) as f:
                                    swap_max_raw = (await f.read()).strip()
                                swap_max = (
                                    None if swap_max_raw == "max" else int(swap_max_raw)
                                )
                            except asyncssh.SFTPError:
                                swap_max = None
                            self.meminfo_data = MemInfo(
                                mem_total=mem_max if mem_max else 0,
                                mem_available=(mem_max - mem_current if mem_max else 0),
                                swap_total=(swap_max if swap_max is not None else 0),
                                swap_free=(
                                    (swap_max - swap_current)
                                    if swap_max is not None
                                    else 0
                                ),
                            )
                        except asyncssh.SFTPError:
                            pass

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

    @property
    def cpu_util(self) -> CpuUtil | None:
        """CPU utilization from cgroupv2, or None if unavailable."""
        return self.cpu_util_data

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
                self.record_success()
                self.start_psi_polling()
                return self.conn
            except Exception:
                self.last_failure = time.monotonic()
                self.backoff = min(self.backoff * 2, self.MAX_BACKOFF)
                self.record_failure()
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


class SSHSubprocessStore(_SSHStoreMixin):
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
        feature_matrix: dict[str, set[str]] | None = None,
        probe: bool = True,
        monitor: bool = True,
        client_keys: list[str | Path | asyncssh.SSHKey] | None = None,
        nix_bin: str = "nix",
    ) -> None:
        super().__init__(
            id=id or f"ssh:{username or ''}@{host}:{port}",
            store_path=store_path,
            max_builds=max_builds,
            max_transfers=max_transfers,
            feature_matrix=feature_matrix,
            probe=probe,
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

        conn = Connection(
            SSHNixReader(proc.stdout, identifier=conn_id),
            SSHNixWriter(proc.stdin, identifier=conn_id),
            conn_id,
        )
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
        feature_matrix: dict[str, set[str]] | None = None,
        probe: bool = True,
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
            feature_matrix=feature_matrix,
            probe=probe,
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
        env["NIX_DATA_DIR"] = str(self.store_path / "share")
        env["NIX_LOG_DIR"] = str(self.store_path / "var/log/nix")
        env["NIX_STATE_DIR"] = str(self.store_path / "var/nix")
        # env["NIX_STORE_DIR"] = str(self.store_path / "store") # this one is evil and should not be changed

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
            UnixNixReader(r, identifier=conn_id),
            UnixNixWriter(w, identifier=conn_id),
            conn_id,
            store_path=self.store_path,
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


class SSHSocketStore(_SSHStoreMixin):
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
        feature_matrix: dict[str, set[str]] | None = None,
        probe: bool = True,
        monitor: bool = True,
        client_keys: list[str | Path | asyncssh.SSHKey] | None = None,
    ) -> None:
        super().__init__(
            id=id or f"ssh-socket:{username or ''}@{host}:{port}",
            max_builds=max_builds,
            max_transfers=max_transfers,
            feature_matrix=feature_matrix,
            probe=probe,
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
        conn = Connection(
            SSHNixReader(r, identifier=conn_id),
            SSHNixWriter(w, identifier=conn_id),
            conn_id,
        )
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
