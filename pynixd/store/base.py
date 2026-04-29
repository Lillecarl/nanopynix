"""
Base Store ABC and pooling logic for pynixd.
"""

from __future__ import annotations

import asyncio
import platform
import time
from abc import ABC, abstractmethod
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Self

import structlog
from cachetools import TTLCache

from .. import wire
from ..monitor import ResourceGate, ResourceMonitor
from ..operations.probe_features import ProbeFeaturesRequest
from ..operations.probe_systems import ProbeSystemsRequest
from ..operations.query_all_valid_paths import QueryAllValidPathsRequest
from ..path_tracker import PathTrackerInstance
from ..system_features import KNOWN_FEATURES, PROBE_SYSTEMS
from .pool import ConnectionPool

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from collections.abc import Set as AbstractSet
    from contextlib import AbstractAsyncContextManager

    from ..connection import ClientConn, Connection
    from ..local_store_db import LocalStoreDB
    from ..operations.base import (
        OpRequest,
        Resp,
        ValidPathInfo,
    )
    from ..psi import CpuUtil, MemInfo
    from ..signing import SecretKey
    from ..store_path import StorePath
    from ..types.ids import StoreId

log = structlog.get_logger(__name__)


_DEFAULT_IDLE_TTL: float = 10.0
_CB_THRESHOLD: int = 3  # failures before cooldown
_CB_MAX_COOLDOWN: float = 300.0  # 5 min max


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
        store_id: StoreId,
        store_path: Path = Path("/"),
        idle_ttl: float = _DEFAULT_IDLE_TTL,
        feature_matrix: dict[str, set[str]] | None = None,
        probe: bool = True,
    ) -> None:
        self.store_id = store_id
        self.store_path = store_path
        self.version: int = wire.PROTOCOL_VERSION
        self.nix_version: str = ""
        self.idle_ttl = idle_ttl
        self.conn_counter = 0

        self.gate = ResourceGate()
        self.pool = ConnectionPool(
            store_id=store_id,
            factory=self._create_conn_with_counter,
            gate=self.gate,
            idle_ttl=idle_ttl,
            on_connection_created=self._on_connection_created,
        )

        self.monitor: ResourceMonitor | None = None
        self._feature_matrix: dict[str, set[str]] | None = feature_matrix
        self._probe = probe
        self.tracker: PathTrackerInstance = PathTrackerInstance(store_id=store_id)
        self.path_info_cache: TTLCache[StorePath, ValidPathInfo] = TTLCache(
            maxsize=10000,
            ttl=300,
        )
        self.consecutive_failures: int = 0
        self.cooldown_until: float = 0.0
        self.db: LocalStoreDB | None = None
        self._features: set[str] = set()
        self.probe_state: ProbeState = ProbeState.NOT_PROBED
        self._probe_event: asyncio.Event = asyncio.Event()
        self._signing_keys: dict[str, SecretKey] = {}
        self._holder_task: asyncio.Task | None = None
        self.draining: bool = False
        self._started: bool = False

    @property
    def features(self) -> AbstractSet[str]:
        """Read-only view of features supported by this store."""
        return self._features

    @property
    def signing_keys(self) -> Mapping[str, SecretKey]:
        """Read-only mapping of signing keys configured on this store."""
        return self._signing_keys

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Explicitly start the store and ensure it is ready for operations.

        Subclasses should override this to perform their specific resource
        setup (like spawning a daemon or connecting SSH) and MUST call
        await super().start().
        """
        if self._started:
            return

        # 1. Ensure the protocol is bootstrapped and discovery is done.
        # This also guarantees that backend daemons have initialized their
        # files (like db.sqlite) by the time probe() returns.
        await self.probe()

        # 2. Synchronize paths to populate the tracker
        try:
            resp = await self.execute(QueryAllValidPathsRequest())
            self.tracker.add_known_paths(resp.paths)
            log.debug("store_paths_synced", store_id=self.store_id, count=len(resp.paths))
        except Exception:
            # Not critical during startup; operations will re-sync if needed
            log.exception("store_paths_sync_failed", store_id=self.store_id)

        # 3. Activate monitoring if configured
        if self.monitor:
            self.monitor.start()

        self._started = True

    def _on_connection_created(self, conn: Connection) -> None:
        """Update store metadata from a newly created connection."""
        self.version = conn.version
        self.nix_version = conn.nix_version
        self._features = conn.features

        # Check for announced feature_matrix to skip probing
        if self._feature_matrix is None and any(f.startswith("feature_matrix:") for f in conn.features):
            fm: dict[str, set[str]] = {}
            for f in conn.features:
                if not f.startswith("feature_matrix:"):
                    continue
                parts = f.split(":")
                if len(parts) == 2:  # feature_matrix:$system
                    system = parts[1]
                    if system not in fm:
                        fm[system] = set()
                elif len(parts) == 3:  # feature_matrix:$system:$feature
                    system, feat = parts[1], parts[2]
                    if system not in fm:
                        fm[system] = set()
                    fm[system].add(feat)

            if fm:
                self._feature_matrix = fm
                self.probe_state = ProbeState.PROBED
                self._probe_event.set()
                log.info(
                    "store_probed_via_handshake",
                    store_id=self.store_id,
                    systems=sorted(fm.keys()),
                    feature_matrix={k: sorted(v) for k, v in fm.items()},
                )

    async def _create_conn_with_counter(self) -> Connection:
        """Wrap create_conn to increment the counter."""
        self.conn_counter += 1
        return await self.create_conn()

    @property
    def db_enabled(self) -> bool:
        """True if this store should use a local SQLite DB for metadata."""
        return True

    @property
    def signing_key_names(self) -> list[str]:
        """List of signing key names configured on this store."""
        return list(self._signing_keys.keys())

    def get_signing_key(self, name: str) -> SecretKey:
        """Get a signing key by name."""
        key = self._signing_keys.get(name)
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
        self,
        system: str,
        features: set[str] | None = None,
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
                store_id=self.store_id,
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
                store_id=self.store_id,
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

        pool = self.build_conn if request.is_build else self.transfer_conn

        async with pool() as conn:
            return await conn.call(
                request,
                client=client,
                suppress_last=suppress_last,
                raise_on_error=raise_on_error,
            )

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

        # Ensure protocol contact and handshake by acquiring a connection.
        # This populates self.version, self.features, etc. and ensures
        # backend resources (daemon/SSH) are fully initialized.
        async with self.pool.acquire("probe"):
            pass

        # If _on_connection_created already set us to PROBED (e.g. via handshake), stop.
        if self.probe_state == ProbeState.PROBED:
            log.debug("probe_skipped_probed_via_handshake", store_id=self.store_id)
            return

        if not self._probe:
            self.probe_state = ProbeState.PROBED
            self._probe_event.set()
            return

        existing_systems = set(self._feature_matrix.keys()) if self._feature_matrix else set()
        existing_features: set[str] = set()
        if self._feature_matrix:
            for feats in self._feature_matrix.values():
                existing_features.update(feats)

        candidate_systems = existing_systems or set(PROBE_SYSTEMS)
        candidate_features = existing_features or set(KNOWN_FEATURES)

        systems_resp = await ProbeSystemsRequest(
            systems=candidate_systems,
        ).execute(self)
        systems = systems_resp.systems

        features_resp = await ProbeFeaturesRequest(
            systems=systems,
            system_features=candidate_features,
        ).execute(self)

        self._feature_matrix = features_resp.feature_matrix

        log.info(
            "store_probed",
            store_id=self.store_id,
            systems=sorted(self._feature_matrix.keys()) if self._feature_matrix else [],
            feature_matrix={k: sorted(v) for k, v in (self._feature_matrix or {}).items()},
        )

        self.probe_state = ProbeState.PROBED
        self._probe_event.set()

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
        return self.pool.active_connections

    @property
    def is_lix(self) -> bool:
        """True if this store is Lix (protocol version 1.35 and version string)."""
        if self.version != wire.proto(1, 35):
            return False

        return "lix" in self.nix_version.lower()

    def build_conn(self) -> AbstractAsyncContextManager[Connection]:
        """Acquire a build connection."""
        return self.pool.acquire("build")

    def transfer_conn(self) -> AbstractAsyncContextManager[Connection]:
        """Acquire a transfer connection."""
        return self.pool.acquire("transfer")

    async def close(self) -> None:
        """Close all pooled connections and stop sweep task."""
        await self.pool.close()

    @property
    def pool_stats(self) -> str:
        """Human-readable pool statistics."""
        return self.pool.stats

    def __repr__(self) -> str:
        return f"{type(self).__name__}(store_id={self.store_id!r}, in_flight={self.in_flight}, pool_stats={self.pool_stats})"


def get_current_system() -> str:
    """Return the current system identifier (e.g., x86_64-linux)."""
    machine = platform.machine()
    system = platform.system().lower()
    if system == "darwin":
        system = "darwin"
    return f"{machine}-{system}"
