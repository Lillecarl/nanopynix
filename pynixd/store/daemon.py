"""DaemonStore — talks to a Nix daemon over the wire protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from .base import Store

if TYPE_CHECKING:
    from ..drv_parser import Derivation
    from ..store_path import StorePath

log = structlog.get_logger(__name__)


class DaemonStore(Store):
    """Store that communicates with a Nix daemon via the wire protocol.

    Every Nix daemon operation is overridden here as a concrete
    implementation that forwards to ``self.call()``.
    Subclasses (LocalDBStore) override specific methods with fast-path
    implementations (SQLite, in-memory caches).
    """

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

        # Decompose: query each path individually
        infos: list[SerdeValidPathInfo] = []
        for path in request.paths:
            resp = await self.query_path_info(
                QueryPathInfoRequest(path=path),
                client=client,
                suppress_last=suppress_last,
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

        # Decompose: walk closure via QueryPathInfos
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

        # Topological sort
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

        # Decompose: read each .drv file locally
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

    async def _probe_daemon(self) -> None:
        """Probe daemon systems and features via build-based probing."""
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

    async def sync_paths(self) -> None:
        """Synchronize known paths from the daemon via serde QueryAllValidPaths."""
        from ..exceptions import BackendError, OpNotImplementedError
        from ..serde import QueryAllValidPathsRequest, QueryValidPathsRequest
        from ..serde import StorePath as SerdeStorePath
        from ..store_path import StorePath as RealStorePath

        try:
            resp = await self.execute(QueryAllValidPathsRequest())
            known: set[RealStorePath] = {RealStorePath(str(p)) for p in resp.paths}
            self.tracker.add_known_paths(known)
            log.info("store_paths_synced", store_id=self.store_id, count=len(resp.paths))
        except (BackendError, OSError, ConnectionError, EOFError, OpNotImplementedError) as e:
            known_paths: set[RealStorePath] | None = None
            if self.tracker.parent is not None and self.tracker.parent.db is not None:
                known_paths = await self.tracker.parent.db.get_known_paths(self.store_id)
            known_paths = known_paths or set(self.tracker.known_paths)

            if not known_paths:
                log.info("store_paths_sync_skipped", store_id=self.store_id)
                return

            log.info(
                "verifying_cached_paths",
                store_id=self.store_id,
                error=str(e),
                count=len(known_paths),
            )
            try:
                serde_paths = {SerdeStorePath(path=str(p)) for p in known_paths}  # pyright: ignore[reportUnhashable]
                verified = await self.execute(
                    QueryValidPathsRequest(paths=serde_paths, substitute=0),
                )
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
            except (BackendError, OSError, ConnectionError, EOFError, OpNotImplementedError) as e2:
                log.warning("path_verification_failed", store_id=self.store_id, error=str(e2))
                self.tracker.add_known_paths(known_paths)
                log.info(
                    "store_paths_sync_cached",
                    store_id=self.store_id,
                    count=len(known_paths),
                )

    async def read_derivation(self, drv_store_path: StorePath | str) -> Derivation | None:
        """Fetch and parse a .drv file from the daemon via NAR."""
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
