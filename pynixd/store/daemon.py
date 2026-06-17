"""DaemonStore — talks to a Nix daemon over the wire protocol."""

from __future__ import annotations

import asyncio
import contextlib
import time
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import structlog

from .. import wire
from ..exceptions import OpNotImplementedError
from ..monitor import ResourceGate, ResourceMonitor
from ..serde.wire_ops import WireRequest
from .base import Store
from .pool import ConnectionPool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from collections.abc import Set as AbstractSet
    from contextlib import AbstractAsyncContextManager

    from ..config import StoreSpecBase
    from ..connection import Connection
    from ..drv_parser import Derivation
    from ..psi import CpuUtil, MemInfo
    from ..store_path import StorePath

log = structlog.get_logger(__name__)
_CB_THRESHOLD: int = 3
_CB_MAX_COOLDOWN: float = 300.0

try:
    import asyncssh
except ImportError:
    _SSH_ERRORS: tuple[type[BaseException], ...] = ()
else:
    _SSH_ERRORS = (asyncssh.misc.Error,)

_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    EOFError,
    OSError,
    TimeoutError,
    *_SSH_ERRORS,
)


class ProbeState(IntEnum):
    NOT_PROBED = 0
    PROBING = 1
    PROBED = 2


class DaemonStore(Store):
    """Store that communicates with a Nix daemon via the wire protocol.

    Adds connection pooling, protocol probing, feature discovery,
    circuit breaking, and reconnect logic on top of the Store ABC.
    """

    def __init__(self, spec: StoreSpecBase) -> None:
        super().__init__(spec)
        self.store_path = getattr(spec, "store_path", Path("/"))
        self.scheduleable = spec.scheduleable
        self.priority = spec.priority
        self.score_penalty = spec.score_penalty
        self.gc_enabled = spec.gc_enabled
        self.gc_max_age = spec.gc_max_age
        self.no_schedule = spec.no_schedule
        self.idle_ttl = spec.idle_ttl
        self.version: int = wire.PROTOCOL_VERSION
        self.nix_version: str = ""
        self.conn_counter = 0

        fm = spec._effective_feature_matrix()
        self._feature_matrix: dict[str, set[str]] | None = fm
        if spec.probe is not None:
            self._probe = spec.probe
        else:
            self._probe = fm is None

        self.gate = ResourceGate()
        self.pool = ConnectionPool(
            store_id=self.store_id,
            factory=self._create_conn_with_counter,
            gate=self.gate,
            idle_ttl=self.idle_ttl,
            on_connection_created=self._on_connection_created,
        )

        self.monitor: ResourceMonitor | None = None
        self.consecutive_failures: int = 0
        self.cooldown_until: float = 0.0
        self._features: set[str] = set()
        self.probe_state: ProbeState = ProbeState.NOT_PROBED
        self._probe_event: anyio.Event = anyio.Event()
        self.draining: bool = False
        self.reconnect_enabled = spec.reconnect
        self.reconnect_min_delay = spec.reconnect_min_delay
        self.reconnect_max_delay = spec.reconnect_max_delay
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_trigger = anyio.Event()
        self._reconnect_delay = self.reconnect_min_delay
        self._on_reconnect: Callable[[], Awaitable[None]] | None = None

    # ── Daemon metadata ─────────────────────────────────────────────

    @property
    def features(self) -> AbstractSet[str]:
        return self._features

    @property
    def feature_matrix(self) -> dict[str, set[str]]:
        if self._feature_matrix is not None:
            return self._feature_matrix
        return {}

    @feature_matrix.setter
    def feature_matrix(self, value: dict[str, set[str]]) -> None:
        self._feature_matrix = value

    @property
    def systems(self) -> set[str] | None:
        fm = self._feature_matrix
        if fm is not None:
            return set(fm.keys()) if fm else None
        return None

    @systems.setter
    def systems(self, value: set[str] | None) -> None:
        if self._feature_matrix is not None:
            if value is None:
                self._feature_matrix = {}
                return
            old = self._feature_matrix
            self._feature_matrix = {s: old.get(s, set()) for s in value}
        elif value is not None:
            self._feature_matrix = {s: set() for s in value}

    def supports_system(self, system: str) -> bool:
        fm = self._feature_matrix
        if fm is None:
            return True
        return system in fm

    def supports_derivation(self, system: str, features: set[str] | None = None) -> bool:
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
        _platform_specific = frozenset({"kvm", "apple-virt"})
        return features.isdisjoint(_platform_specific)

    @property
    def is_lix(self) -> bool:
        if self.version != wire.proto(1, 35):
            return False
        return "lix" in self.nix_version.lower()

    # ── Resource metrics ────────────────────────────────────────────

    @property
    def pressure(self) -> float | None:
        return None

    @property
    def meminfo(self) -> MemInfo | None:
        return None

    @property
    def cpu_util(self) -> CpuUtil | None:
        return None

    # ── Connection pool ─────────────────────────────────────────────

    @property
    def in_flight(self) -> int:
        return self.pool.active_connections

    @property
    def pool_stats(self) -> str:
        return self.pool.stats

    def build_conn(self) -> AbstractAsyncContextManager[Connection]:
        return self.pool.acquire("build")

    def transfer_conn(self) -> AbstractAsyncContextManager[Connection]:
        return self.pool.acquire("transfer")

    async def _create_conn_with_counter(self) -> Connection:
        self.conn_counter += 1
        return await self.create_conn()

    # ── Circuit breaker ─────────────────────────────────────────────

    @property
    def is_healthy(self) -> bool:
        return time.monotonic() >= self.cooldown_until

    def record_success(self) -> None:
        if self.consecutive_failures > 0:
            log.info("store_recovered", store_id=self.store_id, consecutive_failures=self.consecutive_failures)
        self.consecutive_failures = 0
        self.cooldown_until = 0.0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= _CB_THRESHOLD:
            cooldown = min(30 * 2 ** (self.consecutive_failures - _CB_THRESHOLD), _CB_MAX_COOLDOWN)
            self.cooldown_until = time.monotonic() + cooldown
            log.warning(
                "store_cooldown",
                store_id=self.store_id,
                consecutive_failures=self.consecutive_failures,
                cooldown=cooldown,
            )
        self._schedule_reconnect()

    # ── Reconnect loop ──────────────────────────────────────────────

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_trigger.set()

    def _start_reconnect_loop(self) -> None:
        if not self.reconnect_enabled:
            return
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _stop_reconnect_loop(self) -> None:
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with contextlib.suppress(anyio.get_cancelled_exc_class()):
                await self._reconnect_task
            self._reconnect_task = None

    async def _do_reconnect(self) -> None:
        await self.pool.close()
        self.probe_state = ProbeState.NOT_PROBED
        self._probe_event = anyio.Event()
        await self.probe()
        await self.sync_paths()

    async def _reconnect_loop(self) -> None:
        while True:
            await self._reconnect_trigger.wait()
            self._reconnect_trigger = anyio.Event()

            while True:
                delay = self._reconnect_delay
                await anyio.sleep(delay)

                try:
                    await self._do_reconnect()
                except _TRANSPORT_ERRORS:
                    self._reconnect_delay = min(self._reconnect_delay * 2, self.reconnect_max_delay)
                    log.warning("store_reconnect_failed", store_id=self.store_id, next_retry=self._reconnect_delay)
                    continue
                except anyio.get_cancelled_exc_class():
                    raise
                except Exception:
                    log.exception("store_reconnect_unexpected_error", store_id=self.store_id)
                    self._reconnect_delay = min(self._reconnect_delay * 2, self.reconnect_max_delay)
                    continue
                else:
                    self._reconnect_delay = self.reconnect_min_delay
                    self.record_success()
                    log.info("store_reconnected", store_id=self.store_id)
                    if self._on_reconnect:
                        await self._on_reconnect()
                    break

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self, sync_paths: bool = True) -> None:
        if self._started:
            return
        await self.probe()
        if sync_paths:
            await self.sync_paths()
        if self.monitor:
            self.monitor.start()
        self._started = True
        self._start_reconnect_loop()

    async def close(self) -> None:
        await self._stop_reconnect_loop()
        await self.pool.close()

    # ── Execution ───────────────────────────────────────────────────

    async def call(self, request, client=None, suppress_last=False, raise_on_error=False, skip_probe=False):
        if not skip_probe:
            await self.probe()

        is_build = not request.forward if isinstance(request, WireRequest) else request.is_build
        pool = self.build_conn if is_build else self.transfer_conn

        try:
            async with pool() as conn:
                return await conn.call(
                    request, client=client, suppress_last=suppress_last, raise_on_error=raise_on_error
                )
        except _TRANSPORT_ERRORS:
            self.record_failure()
            raise

    async def execute(self, request, client=None, suppress_last=False, skip_probe=False):
        if not skip_probe:
            await self.probe()

        if isinstance(request, WireRequest) and (method_name := self._executors.get(request.op)):
            fn = getattr(self, method_name)
            if result := await fn(request, client=client, suppress_last=suppress_last):
                return result

        if isinstance(request, WireRequest):
            return await self.call(request, client=client, suppress_last=suppress_last)

        return await request.execute(self, client=client, suppress_last=suppress_last)

    # ── Probing ─────────────────────────────────────────────────────

    def _on_connection_created(self, conn: Connection) -> None:
        self.version = conn.version
        self.nix_version = conn.nix_version
        self._features = conn.features

        if self._feature_matrix is None and any(f.startswith("feature_matrix:") for f in conn.features):
            fm: dict[str, set[str]] = {}
            for f in conn.features:
                if not f.startswith("feature_matrix:"):
                    continue
                parts = f.split(":")
                if len(parts) == 2:
                    system = parts[1]
                    if system not in fm:
                        fm[system] = set()
                elif len(parts) == 3:
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

    async def probe(self) -> None:
        if self.probe_state == ProbeState.PROBED:
            return
        if self.probe_state == ProbeState.PROBING:
            await self._probe_event.wait()
            return

        self.probe_state = ProbeState.PROBING

        async with self.pool.acquire("probe"):
            pass

        if self.probe_state == ProbeState.PROBED:
            log.debug("probe_skipped_probed_via_handshake", store_id=self.store_id)
            return

        if not self._probe:
            self.probe_state = ProbeState.PROBED
            self._probe_event.set()
            return

        from ..operations.probe_features import ProbeFeaturesRequest
        from ..operations.probe_systems import ProbeSystemsRequest
        from ..system_features import KNOWN_FEATURES, PROBE_SYSTEMS

        existing_systems = set(self._feature_matrix.keys()) if self._feature_matrix else set()
        existing_features: set[str] = set()
        if self._feature_matrix:
            for feats in self._feature_matrix.values():
                existing_features.update(feats)

        candidate_systems = existing_systems or set(PROBE_SYSTEMS)
        candidate_features = existing_features or set(KNOWN_FEATURES)

        systems_resp = await ProbeSystemsRequest(systems=candidate_systems).execute(self)
        systems = systems_resp.systems

        features_resp = await ProbeFeaturesRequest(systems=systems, system_features=candidate_features).execute(self)
        self._feature_matrix = features_resp.feature_matrix

        log.info(
            "store_probed",
            store_id=self.store_id,
            systems=sorted(self._feature_matrix.keys()) if self._feature_matrix else [],
            feature_matrix={k: sorted(v) for k, v in (self._feature_matrix or {}).items()},
        )

        self.probe_state = ProbeState.PROBED
        self._probe_event.set()

    # ── Path sync ───────────────────────────────────────────────────

    async def sync_paths(self) -> None:
        from ..exceptions import BackendError as _BackendError
        from ..serde import QueryAllValidPathsRequest, QueryValidPathsRequest
        from ..serde import StorePath as SerdeStorePath
        from ..store_path import StorePath as RealStorePath

        try:
            resp = await self.execute(QueryAllValidPathsRequest())
            known: set[RealStorePath] = {RealStorePath(str(p)) for p in resp.paths}
            self.tracker.add_known_paths(known)
            log.info("store_paths_synced", store_id=self.store_id, count=len(resp.paths))
        except (_BackendError, OSError, ConnectionError, EOFError, OpNotImplementedError) as e:
            known_paths: set[RealStorePath] | None = None
            if self.tracker.parent is not None and self.tracker.parent.db is not None:
                known_paths = await self.tracker.parent.db.get_known_paths(self.store_id)
            known_paths = known_paths or set(self.tracker.known_paths)

            if not known_paths:
                log.info("store_paths_sync_skipped", store_id=self.store_id)
                return

            log.info("verifying_cached_paths", store_id=self.store_id, error=str(e), count=len(known_paths))
            try:
                serde_paths = {SerdeStorePath(path=str(p)) for p in known_paths}  # pyright: ignore[reportUnhashable]
                verified = await self.execute(QueryValidPathsRequest(paths=serde_paths, substitute=0))
                known_set = {RealStorePath(str(p)) for p in known_paths}
                verified_set = {RealStorePath(str(p)) for p in verified.paths}
                stale = known_set - verified_set
                if stale:
                    self.tracker.remove_known_paths(stale)
                self.tracker.add_known_paths(verified_set)
                log.info(
                    "store_paths_verified",
                    store_id=self.store_id,
                    total=len(known_paths),
                    verified=len(verified.paths),
                    removed=len(stale),
                )
            except (_BackendError, OSError, ConnectionError, EOFError, OpNotImplementedError) as e2:
                log.warning("path_verification_failed", store_id=self.store_id, error=str(e2))
                self.tracker.add_known_paths(known_paths)
                log.info("store_paths_sync_cached", store_id=self.store_id, count=len(known_paths))

    # ── Standard operations ──────────────────────────────────────────

    async def is_valid_path(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_referrers(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def add_to_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def build_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def ensure_path(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def add_temp_root(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def add_indirect_root(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def find_roots(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def set_options(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def collect_garbage(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_all_valid_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_path_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_path_from_hash_part(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_valid_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_substitutable_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_valid_derivers(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def optimise_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def verify_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def build_derivation(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def add_signatures(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def nar_from_path(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def add_to_store_nar(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_missing(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_derivation_output_map(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def register_drv_output(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_realisation(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def add_multiple_to_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def add_build_log(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def build_paths_with_results(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def add_perm_root(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    # ── Extension operations ─────────────────────────────────────────

    async def pynixd_collect_garbage(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_path_infos(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        from ..serde import QueryPathInfoRequest, QueryPathInfosResponse
        from ..serde.valid_path_info import ValidPathInfo as SerdeValidPathInfo

        if not request.paths:
            return QueryPathInfosResponse(infos=[])

        if "QueryPathInfos" in self.features:
            return await self.call(request, client=client, suppress_last=suppress_last)

        infos: list[SerdeValidPathInfo] = []
        for path in request.paths:
            resp = await self.query_path_info(
                QueryPathInfoRequest(path=path), client=client, suppress_last=suppress_last
            )
            if resp.valid and resp.info is not None:
                infos.append(SerdeValidPathInfo(path=path, info=resp.info))
        return QueryPathInfosResponse(infos=infos)

    async def query_closure(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        if "QueryClosure" in self.features:
            return await self.call(request, client=client, suppress_last=suppress_last)
        from ..serde import QueryClosureResponse

        return QueryClosureResponse(paths=set())

    async def query_closure_with_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        from ..serde import QueryClosureWithInfoResponse, QueryPathInfosRequest
        from ..serde import StorePath as SerdeStorePath

        if not request.paths:
            return QueryClosureWithInfoResponse(infos=[])

        if "QueryClosureWithInfo" in self.features:
            return await self.call(request, client=client, suppress_last=suppress_last)

        pending: set[SerdeStorePath] = set(request.paths)  # pyright: ignore[reportUnhashable]
        all_infos: dict[SerdeStorePath, Any] = {}
        while pending:
            to_fetch = {p for p in pending if p not in all_infos}  # pyright: ignore[reportUnhashable]
            if not to_fetch:
                break
            infos_resp = await self.query_path_infos(
                QueryPathInfosRequest(paths=to_fetch),
                client=client,
                suppress_last=suppress_last,
            )
            new_infos = {info.path: info for info in infos_resp.infos}
            for p in to_fetch:
                if p not in new_infos:
                    raise ValueError(f"Path {p} not found in store closure")
            all_infos.update(new_infos)
            next_pending: set[SerdeStorePath] = set()
            for info in new_infos.values():
                for ref in info.info.references:
                    if ref not in all_infos:
                        next_pending.add(ref)
            pending = next_pending

        sorted_infos: list = []
        visited: set[SerdeStorePath] = set()
        visiting: set[SerdeStorePath] = set()

        def visit(p: SerdeStorePath) -> None:
            if p in visited or p in visiting:
                return
            visiting.add(p)
            info = all_infos[p]
            for ref in info.info.references:
                if ref != p:
                    visit(ref)
            visiting.remove(p)
            visited.add(p)
            sorted_infos.append(info)

        for p in sorted(all_infos.keys(), key=str):
            visit(p)
        return QueryClosureWithInfoResponse(infos=sorted_infos)

    async def query_derivation_output_map_batch(
        self, request: Any, client: Any = None, suppress_last: bool = False
    ) -> Any:
        from ..serde import StorePath as SerdeStorePath
        from ..serde.query_derivation_output_map_batch import DerivationOutputMapBatchResponse

        if not request.drv_paths:
            return DerivationOutputMapBatchResponse(outputs={})

        if "QueryDerivationOutputMapBatch" in self.features:
            return await self.call(request, client=client, suppress_last=suppress_last)

        outputs: dict[SerdeStorePath, dict[str, SerdeStorePath]] = {}
        for drv_path in request.drv_paths:
            try:
                parsed = await self.read_derivation(drv_path)
                if parsed is not None:
                    sp = SerdeStorePath(path=str(drv_path))
                    outs: dict[str, SerdeStorePath | None] = dict(parsed.output_paths().items())  # type: ignore[dict-item]
                    clean: dict[str, SerdeStorePath] = {k: v for k, v in outs.items() if v is not None}
                    outputs[sp] = clean
            except FileNotFoundError:
                pass
        return DerivationOutputMapBatchResponse(outputs=outputs)

    async def sign_path_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def probe_systems(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def probe_features(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    # ── Derivation reading ──────────────────────────────────────────

    async def read_derivation(self, drv_store_path: StorePath | str) -> Derivation | None:
        from ..drv_parser import parse_drv
        from ..nar import NarRegular, parse_nar
        from ..operations.nar_from_path import NarFromPathRequest
        from ..serde import IsValidPathRequest
        from ..serde import StorePath as SerdeStorePath
        from ..store_path import StorePath as OldStorePath

        sp = SerdeStorePath(path=str(drv_store_path))
        old_sp = OldStorePath(str(drv_store_path))

        valid_resp = await self.execute(IsValidPathRequest(path=sp))
        if not valid_resp.valid:
            log.warning("drv_not_found", drv_path=str(drv_store_path), reason="not_valid")
            return None

        resp = await NarFromPathRequest(path=old_sp, nar_size=0).execute(self)
        if not resp.nar_data:
            log.warning("drv_not_found", drv_path=str(drv_store_path), reason="nar_empty")
            return None

        node = parse_nar(resp.nar_data)
        if not isinstance(node, NarRegular):
            log.warning("drv_not_found", drv_path=str(drv_store_path), reason="not_regular_file")
            return None

        return parse_drv(node.contents.decode())
