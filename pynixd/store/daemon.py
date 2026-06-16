"""DaemonStore — talks to a Nix daemon over the wire protocol."""

from __future__ import annotations

from typing import Any

from .base import Store


class DaemonStore(Store):
    """Store that communicates with a Nix daemon via the wire protocol.

    Every Nix daemon operation has an explicit executor method here.
    Subclasses (LocalDBStore) override specific methods with fast-path
    implementations (SQLite, in-memory caches).
    """

    # ── Standard operations (in protocol order) ──────────────────────

    @Store.executor(op=1)
    async def is_valid_path(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=6)
    async def query_referrers(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=7)
    async def add_to_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=9)
    async def build_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=10)
    async def ensure_path(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=11)
    async def add_temp_root(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=12)
    async def add_indirect_root(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=14)
    async def find_roots(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=19)
    async def set_options(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=20)
    async def collect_garbage(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=23)
    async def query_all_valid_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=26)
    async def query_path_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=29)
    async def query_path_from_hash_part(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=31)
    async def query_valid_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=32)
    async def query_substitutable_paths(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=33)
    async def query_valid_derivers(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=34)
    async def optimise_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=35)
    async def verify_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=36)
    async def build_derivation(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=37)
    async def add_signatures(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=38)
    async def nar_from_path(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=39)
    async def add_to_store_nar(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=40)
    async def query_missing(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=41)
    async def query_derivation_output_map(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=42)
    async def register_drv_output(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=43)
    async def query_realisation(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=44)
    async def add_multiple_to_store(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=45)
    async def add_build_log(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=46)
    async def build_paths_with_results(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=47)
    async def add_perm_root(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    # ── Extension operations ─────────────────────────────────────────

    @Store.executor(op=101)
    async def pynixd_collect_garbage(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=103)
    async def query_path_infos(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=104)
    async def query_closure(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=105)
    async def query_closure_with_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=106)
    async def query_derivation_output_map_batch(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=107)
    async def sign_path_info(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=108)
    async def probe_systems(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)

    @Store.executor(op=109)
    async def probe_features(self, request: Any, client: Any = None, suppress_last: bool = False) -> Any:
        return await self.call(request, client=client, suppress_last=suppress_last)
