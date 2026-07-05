"""Store facade — async Python wrapper over Nix store operations.

All methods return Pydantic models.  The facade is execution-backend-agnostic:
it delegates every call to ``self._pool``, which may be in-process or
multiprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from nanopynix.models import BuildResult, MissingInfo, PathInfo, StorePath

import nanopynix_store  # BuildMode enum

if TYPE_CHECKING:
    from nanopynix._pool import _WorkerManager

_StorePathList = TypeAdapter(list[StorePath])
_BuildResultList = TypeAdapter(list[BuildResult])


def _to_str(path: StorePath | str) -> str:
    """Coerce a StorePath or str to a store-path string."""
    return path.to_string if isinstance(path, StorePath) else path


def _to_strs(paths: list[StorePath | str]) -> list[str]:
    """Coerce a list of StorePath|str to a list of store-path strings."""
    return [p.to_string if isinstance(p, StorePath) else p for p in paths]


@dataclass
class Store:
    """Async wrapper around a Nix store.

    Create via ``Nix(store_uri=...).open()``, not directly.
    """

    _pool: _WorkerManager

    # ── Identity ──────────────────────────────────────────────────

    async def get_uri(self) -> str:
        return await self._pool.call("store", "get_uri", [])

    async def get_store_dir(self) -> str:
        return await self._pool.call("store", "get_store_dir", [])

    # ── StorePath parsing ─────────────────────────────────────────

    async def parse_store_path(self, path: str) -> StorePath:
        data = await self._pool.call("store", "parse_store_path", [path])
        return StorePath.model_validate(data)

    async def is_valid_path(self, path: StorePath | str) -> bool:
        s = _to_str(path)
        return await self._pool.call("store", "is_valid_path", [s])

    async def follow_links_to_store_path(self, path: str) -> StorePath:
        data = await self._pool.call("store", "follow_links_to_store_path", [path])
        return StorePath.model_validate(data)

    # ── Path info ─────────────────────────────────────────────────

    async def query_path_info(self, path: StorePath | str) -> PathInfo:
        s = _to_str(path)
        data = await self._pool.call("store", "query_path_info", [s])
        return PathInfo.model_validate(data)

    async def query_path_from_hash_part(self, hash_part: str) -> StorePath:
        data = await self._pool.call("store", "query_path_from_hash_part", [hash_part])
        return StorePath.model_validate(data)

    # ── Closures ──────────────────────────────────────────────────

    async def compute_fs_closure(
        self,
        path: StorePath | str,
        flip: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[StorePath]:
        s = _to_str(path)
        data = await self._pool.call("store", "compute_fs_closure",
            [s, flip, include_outputs, include_derivers])
        return _StorePathList.validate_python(data)

    async def query_missing(self, paths: list[StorePath | str]) -> MissingInfo:
        strs = _to_strs(paths)
        data = await self._pool.call("store", "query_missing", [strs])
        return MissingInfo.model_validate(data)

    # ── Derivations ───────────────────────────────────────────────

    async def query_derivation_outputs(self, path: StorePath | str) -> list[StorePath]:
        s = _to_str(path)
        data = await self._pool.call("store", "query_derivation_outputs", [s])
        return _StorePathList.validate_python(data)

    async def query_valid_derivers(self, path: StorePath | str) -> list[StorePath]:
        s = _to_str(path)
        data = await self._pool.call("store", "query_valid_derivers", [s])
        return _StorePathList.validate_python(data)

    # ── Bulk queries ──────────────────────────────────────────────

    async def query_all_valid_paths(self) -> list[StorePath]:
        data = await self._pool.call("store", "query_all_valid_paths", [])
        return _StorePathList.validate_python(data)

    async def query_referrers(self, path: StorePath | str) -> list[StorePath]:
        s = _to_str(path)
        data = await self._pool.call("store", "query_referrers", [s])
        return _StorePathList.validate_python(data)

    async def query_substitutable_paths(self, paths: list[StorePath | str]) -> list[StorePath]:
        strs = _to_strs(paths)
        data = await self._pool.call("store", "query_substitutable_paths", [strs])
        return _StorePathList.validate_python(data)

    # ── Build ─────────────────────────────────────────────────────

    async def build_paths_with_results(
        self, paths: list[StorePath | str],
    ) -> list[BuildResult]:
        strs = _to_strs(paths)
        data = await self._pool.call("store", "build_paths_with_results", [strs])
        return _BuildResultList.validate_python(data)

    async def read_derivation(
        self, drv_path: StorePath | str,
    ) -> dict:
        s = _to_str(drv_path)
        return await self._pool.call("store", "read_derivation", [s])

    async def build_derivation(
        self, drv_path: StorePath | str,
        build_mode: nanopynix_store.BuildMode | int = nanopynix_store.BuildMode.Normal,
    ) -> BuildResult:
        s = _to_str(drv_path)
        mode = int(build_mode)
        data = await self._pool.call("store", "build_derivation", [s, mode])
        return BuildResult.model_validate(data)

    # ── GC ────────────────────────────────────────────────────────

    async def add_temp_root(self, path: StorePath | str) -> None:
        s = _to_str(path)
        await self._pool.call("store", "add_temp_root", [s])
