"""Store facade — async Python wrapper over Nix store operations.

``StoreHandle`` is a context manager returned by ``Session.store()``.
It delegates every call to the session's ``_WorkerManager`` and carries
a ``_session_id`` that ``Eval`` checks at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import nanopynix_store  # BuildMode enum
from nanopynix import _protocol as rpc
from nanopynix.models import BuildResult, Derivation, Input, MissingInfo, PathInfo, StorePath

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nanopynix._pool import _WorkerManager


def _to_str(path: StorePath | str) -> str:
    """Coerce a StorePath or str to a store-path string."""
    return path.to_string if isinstance(path, StorePath) else path


def _to_strs(paths: Sequence[StorePath | str]) -> list[str]:
    """Coerce a list of StorePath|str to a list of store-path strings."""
    return [p.to_string if isinstance(p, StorePath) else p for p in paths]


def _build_mode_value(build_mode: nanopynix_store.BuildMode | int) -> int:
    return build_mode if isinstance(build_mode, int) else build_mode.value


class StoreHandle:
    """Async wrapper around a Nix store — context manager for lifecycle.

    Created via ``Session.store(uri="daemon")``.  Carries a ``_session_id``
    that ``Eval`` checks at runtime to prevent cross-session usage.

    Usage::

        async with session.store() as store:
            info = await store.query_path_info(sp)
            print(info.nar_hash)
    """

    __slots__ = ("_active", "_pool", "_session_id", "_uri")

    def __init__(self, pool: _WorkerManager, uri: str, session_id: str) -> None:
        self._pool = pool
        self._uri = uri
        self._session_id = session_id
        self._active = False

    # ── lifecycle ──────────────────────────────────────────────────

    async def open(self) -> None:
        """Activate the handle (called by context manager or manually)."""
        self._active = True

    async def close(self) -> None:
        """Deactivate the handle."""
        self._active = False

    async def __aenter__(self) -> StoreHandle:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    def _check_active(self) -> None:
        if not self._active:
            raise RuntimeError("StoreHandle is closed — use 'async with session.store() as store:'")

    # ── Identity ──────────────────────────────────────────────────

    async def get_uri(self) -> str:
        self._check_active()
        return await self._pool.request(rpc.GetUri())

    async def get_store_dir(self) -> str:
        self._check_active()
        return await self._pool.request(rpc.GetStoreDir())

    # ── StorePath parsing ─────────────────────────────────────────

    async def parse_store_path(self, path: str) -> StorePath:
        self._check_active()
        return await self._pool.request(rpc.ParseStorePath(path=path))

    async def is_valid_path(self, path: StorePath | str) -> bool:
        self._check_active()
        s = _to_str(path)
        return await self._pool.request(rpc.IsValidPath(path=s))

    async def follow_links_to_store_path(self, path: str) -> StorePath:
        self._check_active()
        return await self._pool.request(rpc.FollowLinksToStorePath(path=path))

    # ── Path info ─────────────────────────────────────────────────

    async def query_path_info(self, path: StorePath | str) -> PathInfo:
        self._check_active()
        s = _to_str(path)
        return await self._pool.request(rpc.QueryPathInfo(path=s))

    async def query_path_from_hash_part(
        self, hash_part: str
    ) -> StorePath | None:
        self._check_active()
        return await self._pool.request(rpc.QueryPathFromHashPart(hash_part=hash_part))

    # ── Closures ──────────────────────────────────────────────────

    async def compute_fs_closure(
        self,
        path: StorePath | str,
        flip: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[StorePath]:
        self._check_active()
        s = _to_str(path)
        return await self._pool.request(
            rpc.ComputeFsClosure(
                path=s,
                flip_direction=flip,
                include_outputs=include_outputs,
                include_derivers=include_derivers,
            )
        )

    async def query_missing(
        self, paths: list[StorePath | str]
    ) -> MissingInfo:
        self._check_active()
        strs = _to_strs(paths)
        return await self._pool.request(rpc.QueryMissing(paths=strs))

    # ── Derivations ───────────────────────────────────────────────

    async def query_derivation_outputs(
        self, path: StorePath | str
    ) -> list[StorePath]:
        self._check_active()
        s = _to_str(path)
        return await self._pool.request(rpc.QueryDerivationOutputs(path=s))

    async def query_valid_derivers(
        self, path: StorePath | str
    ) -> list[StorePath]:
        self._check_active()
        s = _to_str(path)
        return await self._pool.request(rpc.QueryValidDerivers(path=s))

    # ── Bulk queries ──────────────────────────────────────────────

    async def query_all_valid_paths(self) -> list[StorePath]:
        self._check_active()
        return await self._pool.request(rpc.QueryAllValidPaths())

    async def query_referrers(
        self, path: StorePath | str
    ) -> list[StorePath]:
        self._check_active()
        s = _to_str(path)
        return await self._pool.request(rpc.QueryReferrers(path=s))

    async def query_substitutable_paths(
        self, paths: Sequence[StorePath | str]
    ) -> list[StorePath]:
        self._check_active()
        strs = _to_strs(paths)
        return await self._pool.request(rpc.QuerySubstitutablePaths(paths=strs))

    # ── Build ─────────────────────────────────────────────────────

    async def build_paths_with_results(
        self,
        paths: Sequence[StorePath | str],
    ) -> list[BuildResult]:
        self._check_active()
        strs = _to_strs(paths)
        return await self._pool.request(rpc.BuildPathsWithResults(paths=strs))

    async def read_derivation(
        self,
        drv_path: StorePath | str,
    ) -> Derivation:
        self._check_active()
        s = _to_str(drv_path)
        return await self._pool.request(rpc.ReadDerivation(path=s))

    async def build_derivation(
        self,
        drv_path: StorePath | str,
        build_mode: nanopynix_store.BuildMode | int = nanopynix_store.BuildMode.Normal,
    ) -> BuildResult:
        self._check_active()
        s = _to_str(drv_path)
        mode = _build_mode_value(build_mode)
        return await self._pool.request(rpc.BuildDerivation(path=s, build_mode=mode))

    # ── GC ────────────────────────────────────────────────────────

    async def add_temp_root(self, path: StorePath | str) -> None:
        self._check_active()
        s = _to_str(path)
        return await self._pool.request(rpc.AddTempRoot(path=s))

    # ── Fetchers ──────────────────────────────────────────────────

    async def fetch_from_url(self, url: str) -> Input:
        self._check_active()
        return await self._pool.request(rpc.FetchFromUrl(url=url))

    async def fetch_from_attrs(
        self, attrs: dict[str, str | int | bool]
    ) -> Input:
        self._check_active()
        return await self._pool.request(rpc.FetchFromAttrs(attrs=attrs))


# Backward-compatible alias
Store = StoreHandle
