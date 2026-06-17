"""
Base Store ABC for pynixd.

Defines the minimal contract that every store backend must fulfill:
path tracking, signing keys, operation execution, and lifecycle
management.  Daemon-specific logic (connection pooling, probing,
feature matrices, circuit breaking) lives in DaemonStore.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Self, overload

import structlog
from cachetools import TTLCache

from ..path_tracker import PathTrackerInstance

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from ..config import StoreSpecBase
    from ..connection import ClientConn, Connection
    from ..drv_parser import Derivation
    from ..operations.base import (
        OpRequest,
        Resp,
        ValidPathInfo,
    )
    from ..serde.wire_ops import WireRequest
    from ..signing import SecretKey
    from ..store_path import StorePath
    from ..types.aliases import StorePathSet
    from ..types.ids import StoreId


log = structlog.get_logger(__name__)


class Store(ABC):
    """Minimal store contract.

    Subclasses implement create_conn() and the operation executors.
    Daemon-backed stores extend DaemonStore, which adds connection
    pooling, probing, and circuit-breaking.
    """

    _executors: ClassVar[dict[int, str]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name, method in cls.__dict__.items():
            if callable(method) and hasattr(method, "_pynixd_op"):
                cls._executors[method._pynixd_op] = name  # type: ignore[union-attr]

    def __init__(self, spec: StoreSpecBase) -> None:
        if spec.store_id is None:
            raise RuntimeError("store_id must be set on the spec before Store construction")
        self.store_id: StoreId = spec.store_id
        self._signing_keys: dict[str, SecretKey] = {}
        self.tracker = PathTrackerInstance(store_id=self.store_id)
        self.path_info_cache: TTLCache[StorePath, ValidPathInfo] = TTLCache(  # type: ignore[type-var]
            maxsize=10000,
            ttl=300,
        )
        self._started: bool = False

    # ── Lifecycle ───────────────────────────────────────────────────

    @abstractmethod
    async def start(self, sync_paths: bool = True) -> None:
        """Explicitly start the store and ensure it is ready for operations."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close all resources."""
        ...

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ── Connection / execution ──────────────────────────────────────

    @abstractmethod
    async def create_conn(self) -> Connection:
        """Create transport, construct Connection, and connect it."""
        ...

    @overload
    async def call(
        self,
        request: WireRequest,
        client: ClientConn | None = None,
        suppress_last: bool = False,
        raise_on_error: bool = False,
        skip_probe: bool = False,
    ) -> Any: ...

    @overload
    async def call(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
        raise_on_error: bool = False,
        skip_probe: bool = False,
    ) -> Resp: ...

    @abstractmethod
    async def call(
        self,
        request: OpRequest[Resp] | WireRequest,
        client: ClientConn | None = None,
        suppress_last: bool = False,
        raise_on_error: bool = False,
        skip_probe: bool = False,
    ) -> Any:
        """Send an operation to this store."""
        ...

    @overload
    async def execute(
        self,
        request: WireRequest,
        client: ClientConn | None = None,
        suppress_last: bool = False,
        skip_probe: bool = False,
    ) -> Any: ...

    @overload
    async def execute(
        self,
        request: OpRequest[Resp],
        client: ClientConn | None = None,
        suppress_last: bool = False,
        skip_probe: bool = False,
    ) -> Resp: ...

    @abstractmethod
    async def execute(
        self,
        request: OpRequest[Resp] | WireRequest,
        client: ClientConn | None = None,
        suppress_last: bool = False,
        skip_probe: bool = False,
    ) -> Any:
        """Execute an operation on this store."""
        ...

    @abstractmethod
    async def read_derivation(self, drv_store_path: StorePath | str) -> Derivation | None:
        """Fetch and parse a .drv file from this store."""
        ...

    # ── Signing ─────────────────────────────────────────────────────

    @property
    def signing_keys(self) -> Mapping[str, SecretKey]:
        """Read-only mapping of signing keys configured on this store."""
        return self._signing_keys

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

    # ── Path tracking ──────────────────────────────────────────────

    def has_path(self, path: StorePath) -> bool:
        return path in self.tracker.known_paths

    def has_all_paths(self, paths: StorePathSet) -> bool:
        return paths.issubset(self.tracker.known_paths)

    def count_common_paths(self, paths: StorePathSet) -> int:
        return len(paths & self.tracker.known_paths)

    def add_path_info(self, info: ValidPathInfo) -> None:
        self.path_info_cache[info.path] = info

    def add_path_infos(self, infos: Iterable[ValidPathInfo]) -> None:
        for info in infos:
            self.path_info_cache[info.path] = info

    def get_path_info(self, path: StorePath) -> ValidPathInfo | None:
        return self.path_info_cache.get(path)

    # ── Executor infrastructure ─────────────────────────────────────

    @classmethod
    def _register_executor(cls, op: int, name: str) -> None:
        """Register a method as the executor for an operation."""
        cls._executors[op] = name

    @staticmethod
    def executor(op: int):
        """Decorator: register a method as the executor for an operation."""

        def decorator(method):
            method._pynixd_op = op
            return method

        return decorator

    # ── Operation executors (abstract — subclasses MUST override) ───

    @executor(op=1)
    @abstractmethod
    async def is_valid_path(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """IsValidPath (op 1)."""
        raise NotImplementedError

    @executor(op=6)
    @abstractmethod
    async def query_referrers(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryReferrers (op 6)."""
        raise NotImplementedError

    @executor(op=7)
    @abstractmethod
    async def add_to_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """AddToStore (op 7)."""
        raise NotImplementedError

    @executor(op=9)
    @abstractmethod
    async def build_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """BuildPaths (op 9)."""
        raise NotImplementedError

    @executor(op=10)
    @abstractmethod
    async def ensure_path(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """EnsurePath (op 10)."""
        raise NotImplementedError

    @executor(op=11)
    @abstractmethod
    async def add_temp_root(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """AddTempRoot (op 11)."""
        raise NotImplementedError

    @executor(op=12)
    @abstractmethod
    async def add_indirect_root(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """AddIndirectRoot (op 12)."""
        raise NotImplementedError

    @executor(op=14)
    @abstractmethod
    async def find_roots(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """FindRoots (op 14)."""
        raise NotImplementedError

    @executor(op=19)
    @abstractmethod
    async def set_options(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """SetOptions (op 19)."""
        raise NotImplementedError

    @executor(op=20)
    @abstractmethod
    async def collect_garbage(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """CollectGarbage (op 20)."""
        raise NotImplementedError

    @executor(op=23)
    @abstractmethod
    async def query_all_valid_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryAllValidPaths (op 23)."""
        raise NotImplementedError

    @executor(op=26)
    @abstractmethod
    async def query_path_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryPathInfo (op 26)."""
        raise NotImplementedError

    @executor(op=29)
    @abstractmethod
    async def query_path_from_hash_part(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryPathFromHashPart (op 29)."""
        raise NotImplementedError

    @executor(op=31)
    @abstractmethod
    async def query_valid_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryValidPaths (op 31)."""
        raise NotImplementedError

    @executor(op=32)
    @abstractmethod
    async def query_substitutable_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QuerySubstitutablePaths (op 32)."""
        raise NotImplementedError

    @executor(op=33)
    @abstractmethod
    async def query_valid_derivers(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryValidDerivers (op 33)."""
        raise NotImplementedError

    @executor(op=34)
    @abstractmethod
    async def optimise_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """OptimiseStore (op 34)."""
        raise NotImplementedError

    @executor(op=35)
    @abstractmethod
    async def verify_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """VerifyStore (op 35)."""
        raise NotImplementedError

    @executor(op=36)
    @abstractmethod
    async def build_derivation(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """BuildDerivation (op 36)."""
        raise NotImplementedError

    @executor(op=37)
    @abstractmethod
    async def add_signatures(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """AddSignatures (op 37)."""
        raise NotImplementedError

    @executor(op=38)
    @abstractmethod
    async def nar_from_path(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """NarFromPath (op 38)."""
        raise NotImplementedError

    @executor(op=39)
    @abstractmethod
    async def add_to_store_nar(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """AddToStoreNar (op 39)."""
        raise NotImplementedError

    @executor(op=40)
    @abstractmethod
    async def query_missing(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryMissing (op 40)."""
        raise NotImplementedError

    @executor(op=41)
    @abstractmethod
    async def query_derivation_output_map(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryDerivationOutputMap (op 41)."""
        raise NotImplementedError

    @executor(op=42)
    @abstractmethod
    async def register_drv_output(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """RegisterDrvOutput (op 42)."""
        raise NotImplementedError

    @executor(op=43)
    @abstractmethod
    async def query_realisation(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryRealisation (op 43)."""
        raise NotImplementedError

    @executor(op=44)
    @abstractmethod
    async def add_multiple_to_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """AddMultipleToStore (op 44)."""
        raise NotImplementedError

    @executor(op=45)
    @abstractmethod
    async def add_build_log(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """AddBuildLog (op 45)."""
        raise NotImplementedError

    @executor(op=46)
    @abstractmethod
    async def build_paths_with_results(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """BuildPathsWithResults (op 46)."""
        raise NotImplementedError

    @executor(op=47)
    @abstractmethod
    async def add_perm_root(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """AddPermRoot (op 47)."""
        raise NotImplementedError

    # ── Custom/pynixd extension operations ───────────────────────────

    @executor(op=101)
    @abstractmethod
    async def pynixd_collect_garbage(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """PynixdCollectGarbage (op 101)."""
        raise NotImplementedError

    @executor(op=103)
    @abstractmethod
    async def query_path_infos(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryPathInfos (op 103)."""
        raise NotImplementedError

    @executor(op=104)
    @abstractmethod
    async def query_closure(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryClosure (op 104)."""
        raise NotImplementedError

    @executor(op=105)
    @abstractmethod
    async def query_closure_with_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """QueryClosureWithInfo (op 105)."""
        raise NotImplementedError

    @executor(op=106)
    @abstractmethod
    async def query_derivation_output_map_batch(
        self, request: Any, client: Any = None, suppress_last: bool = False
    ) -> Any:
        """QueryDerivationOutputMapBatch (op 106)."""
        raise NotImplementedError

    @executor(op=107)
    @abstractmethod
    async def sign_path_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """SignPathInfo (op 107)."""
        raise NotImplementedError

    @executor(op=108)
    @abstractmethod
    async def probe_systems(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """ProbeSystems (op 108)."""
        raise NotImplementedError

    @executor(op=109)
    @abstractmethod
    async def probe_features(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        """ProbeFeatures (op 109)."""
        raise NotImplementedError


def get_current_system() -> str:
    """Return the current system identifier (e.g., x86_64-linux)."""
    import platform

    machine = platform.machine()
    system = platform.system().lower()
    if system == "darwin":
        system = "darwin"
    return f"{machine}-{system}"


# Populate Store._executors from methods decorated with @executor (__init_subclass__
# only fires for subclasses, not the base class itself).
for _name, _method in Store.__dict__.items():
    if callable(_method) and (op := getattr(_method, "_pynixd_op", None)) is not None:
        Store._executors[op] = _name
