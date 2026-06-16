"""DaemonStore — talks to a Nix daemon over the wire protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from ..store_path import StorePath
from .base import Store

if TYPE_CHECKING:
    from ..drv_parser import Derivation

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
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_closure(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_closure_with_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def query_derivation_output_map_batch(
        self, request: Any, client: Any = None, suppress_last: bool = False
    ) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def sign_path_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def probe_systems(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    async def probe_features(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    # ── Derivation reading ──────────────────────────────────────────

    async def read_derivation(self, drv_store_path: StorePath | str) -> Derivation | None:
        """Fetch and parse a .drv file from the daemon via NAR."""
        from ..drv_parser import parse_drv
        from ..nar import NarRegular, parse_nar
        from ..operations.is_valid_path import IsValidPathRequest
        from ..operations.nar_from_path import NarFromPathRequest

        sp = StorePath(str(drv_store_path))

        valid = (await IsValidPathRequest(path=sp).execute(self)).valid
        if not valid:
            log.warning("drv_not_found", drv_path=str(drv_store_path), reason="not_valid")
            return None

        resp = await NarFromPathRequest(path=sp, nar_size=0).execute(self)
        if not resp.nar_data:
            log.warning("drv_not_found", drv_path=str(drv_store_path), reason="nar_empty")
            return None

        node = parse_nar(resp.nar_data)
        if not isinstance(node, NarRegular):
            log.warning("drv_not_found", drv_path=str(drv_store_path), reason="not_regular_file")
            return None

        return parse_drv(node.contents.decode())
