"""Store facade — async Python wrapper over Nix store operations.

All methods return Pydantic models.  The facade is execution-backend-agnostic:
it delegates every call to ``self._imp``, which may be in-process or
multiprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter

from nanopynix.models import BuildResult, MissingInfo, PathInfo, StorePath

_StorePathList = TypeAdapter(list[StorePath])
_BuildResultList = TypeAdapter(list[BuildResult])


@dataclass
class Store:
    """Async wrapper around a Nix store.

    Create via ``Nix(store_uri=...).open()``, not directly.
    """

    _imp: Any  # WorkerPool

    # ── Identity ──────────────────────────────────────────────────

    async def get_uri(self) -> str:
        return await self._imp.store_get_uri()

    async def get_store_dir(self) -> str:
        return await self._imp.store_get_store_dir()

    # ── StorePath parsing ─────────────────────────────────────────

    async def parse_store_path(self, path: str) -> StorePath:
        data = await self._imp.store_parse_store_path(path)
        return StorePath.model_validate(data)

    async def is_valid_path(self, path: StorePath | str) -> bool:
        s = path.to_string if isinstance(path, StorePath) else path
        return await self._imp.store_is_valid_path(s)

    async def follow_links_to_store_path(self, path: str) -> StorePath:
        data = await self._imp.store_follow_links_to_store_path(path)
        return StorePath.model_validate(data)

    # ── Path info ─────────────────────────────────────────────────

    async def query_path_info(self, path: StorePath | str) -> PathInfo:
        s = path.to_string if isinstance(path, StorePath) else path
        data = await self._imp.store_query_path_info(s)
        return PathInfo.model_validate(data)

    async def query_path_from_hash_part(self, hash_part: str) -> StorePath:
        data = await self._imp.store_query_path_from_hash_part(hash_part)
        return StorePath.model_validate(data)

    # ── Closures ──────────────────────────────────────────────────

    async def compute_fs_closure(
        self,
        path: StorePath | str,
        flip: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[StorePath]:
        s = path.to_string if isinstance(path, StorePath) else path
        data = await self._imp.store_compute_fs_closure(
            s, flip, include_outputs, include_derivers,
        )
        return _StorePathList.validate_python(data)

    async def query_missing(self, paths: list[StorePath | str]) -> MissingInfo:
        strs = [
            p.to_string if isinstance(p, StorePath) else p for p in paths
        ]
        data = await self._imp.store_query_missing(strs)
        return MissingInfo.model_validate(data)

    # ── Derivations ───────────────────────────────────────────────

    async def query_derivation_outputs(self, path: StorePath | str) -> list[StorePath]:
        s = path.to_string if isinstance(path, StorePath) else path
        data = await self._imp.store_query_derivation_outputs(s)
        return _StorePathList.validate_python(data)

    async def query_valid_derivers(self, path: StorePath | str) -> list[StorePath]:
        s = path.to_string if isinstance(path, StorePath) else path
        data = await self._imp.store_query_valid_derivers(s)
        return _StorePathList.validate_python(data)

    # ── Bulk queries ──────────────────────────────────────────────

    async def query_all_valid_paths(self) -> list[StorePath]:
        data = await self._imp.store_query_all_valid_paths()
        return _StorePathList.validate_python(data)

    async def query_referrers(self, path: StorePath | str) -> list[StorePath]:
        s = path.to_string if isinstance(path, StorePath) else path
        data = await self._imp.store_query_referrers(s)
        return _StorePathList.validate_python(data)

    async def query_substitutable_paths(self, paths: list[StorePath | str]) -> list[StorePath]:
        strs = [
            p.to_string if isinstance(p, StorePath) else p for p in paths
        ]
        data = await self._imp.store_query_substitutable_paths(strs)
        return _StorePathList.validate_python(data)

    # ── Build ─────────────────────────────────────────────────────

    async def build_paths_with_results(
        self, paths: list[StorePath | str],
    ) -> list[BuildResult]:
        strs = [
            p.to_string if isinstance(p, StorePath) else p for p in paths
        ]
        data = await self._imp.store_build_paths_with_results(strs)
        return _BuildResultList.validate_python(data)

    async def read_derivation(
        self, drv_path: StorePath | str,
    ) -> dict:
        s = drv_path.to_string if isinstance(drv_path, StorePath) else drv_path
        return await self._imp.store_read_derivation(s)

    async def build_derivation(
        self, drv_path: StorePath | str, build_mode: int = 0,
    ) -> BuildResult:
        s = drv_path.to_string if isinstance(drv_path, StorePath) else drv_path
        data = await self._imp.store_build_derivation(s, build_mode)
        return BuildResult.model_validate(data)

    # ── GC ────────────────────────────────────────────────────────

    async def add_temp_root(self, path: StorePath | str) -> None:
        s = path.to_string if isinstance(path, StorePath) else path
        await self._imp.store_add_temp_root(s)
