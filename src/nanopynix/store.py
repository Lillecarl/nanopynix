"""Store facade — async Python wrapper over Nix store operations.

``StoreHandle`` is a context manager returned by ``Session.store()``.
It delegates every call to the session's ``_WorkerManager`` and carries
a ``_session_id`` that ``Eval`` checks at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, TypeVar, cast

from pydantic import BaseModel, TypeAdapter

from nanopynix._rpc import identity, manager_call
from nanopynix.models import BuildResult, Derivation, Input, MissingInfo, PathInfo, StorePath

import nanopynix_store  # BuildMode enum

if TYPE_CHECKING:
    from nanopynix._pool import _WorkerManager

_StorePathList = TypeAdapter(list[StorePath])
_BuildResultList = TypeAdapter(list[BuildResult])
M = TypeVar("M", bound=BaseModel)


def _to_str(path: StorePath | str) -> str:
    """Coerce a StorePath or str to a store-path string."""
    return path.to_string if isinstance(path, StorePath) else path


def _to_strs(paths: list[StorePath | str]) -> list[str]:
    """Coerce a list of StorePath|str to a list of store-path strings."""
    return [p.to_string if isinstance(p, StorePath) else p for p in paths]


def _model_adapter(model: type[M]) -> Callable[[Any], M]:
    return model.model_validate


class StoreHandle:
    """Async wrapper around a Nix store — context manager for lifecycle.

    Created via ``Session.store(uri="daemon")``.  Carries a ``_session_id``
    that ``Eval`` checks at runtime to prevent cross-session usage.

    Usage::

        async with session.store() as store:
            info = await store.query_path_info(sp)
            print(info.nar_hash)
    """

    __slots__ = ("_pool", "_uri", "_session_id", "_active")

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
        return await manager_call(self._pool, "store", "get_uri", [], str)

    async def get_store_dir(self) -> str:
        self._check_active()
        return await manager_call(self._pool, "store", "get_store_dir", [], str)

    # ── StorePath parsing ─────────────────────────────────────────

    async def parse_store_path(self, path: str) -> StorePath:
        self._check_active()
        return await manager_call(
            self._pool, "store", "parse_store_path", [path], _model_adapter(StorePath)
        )

    async def is_valid_path(self, path: StorePath | str) -> bool:
        self._check_active()
        s = _to_str(path)
        return await manager_call(self._pool, "store", "is_valid_path", [s], bool)

    async def follow_links_to_store_path(self, path: str) -> StorePath:
        self._check_active()
        return await manager_call(
            self._pool, "store", "follow_links_to_store_path", [path], _model_adapter(StorePath)
        )

    # ── Path info ─────────────────────────────────────────────────

    async def query_path_info(self, path: StorePath | str) -> PathInfo:
        self._check_active()
        s = _to_str(path)
        return await manager_call(
            self._pool, "store", "query_path_info", [s], _model_adapter(PathInfo)
        )

    async def query_path_from_hash_part(
        self, hash_part: str
    ) -> StorePath | None:
        self._check_active()

        def adapt(value):
            return None if value is None else StorePath.model_validate(value)

        return await manager_call(self._pool, "store", "query_path_from_hash_part", [hash_part], adapt)

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
        return await manager_call(
            self._pool,
            "store",
            "compute_fs_closure",
            [s, flip, include_outputs, include_derivers],
            _StorePathList.validate_python,
        )

    async def query_missing(
        self, paths: list[StorePath | str]
    ) -> MissingInfo:
        self._check_active()
        strs = _to_strs(paths)
        return await manager_call(
            self._pool, "store", "query_missing", [strs], _model_adapter(MissingInfo)
        )

    # ── Derivations ───────────────────────────────────────────────

    async def query_derivation_outputs(
        self, path: StorePath | str
    ) -> list[StorePath]:
        self._check_active()
        s = _to_str(path)
        return await manager_call(
            self._pool, "store", "query_derivation_outputs", [s], _StorePathList.validate_python
        )

    async def query_valid_derivers(
        self, path: StorePath | str
    ) -> list[StorePath]:
        self._check_active()
        s = _to_str(path)
        return await manager_call(
            self._pool, "store", "query_valid_derivers", [s], _StorePathList.validate_python
        )

    # ── Bulk queries ──────────────────────────────────────────────

    async def query_all_valid_paths(self) -> list[StorePath]:
        self._check_active()
        return await manager_call(
            self._pool, "store", "query_all_valid_paths", [], _StorePathList.validate_python
        )

    async def query_referrers(
        self, path: StorePath | str
    ) -> list[StorePath]:
        self._check_active()
        s = _to_str(path)
        return await manager_call(
            self._pool, "store", "query_referrers", [s], _StorePathList.validate_python
        )

    async def query_substitutable_paths(
        self, paths: list[StorePath | str]
    ) -> list[StorePath]:
        self._check_active()
        strs = _to_strs(paths)
        return await manager_call(
            self._pool, "store", "query_substitutable_paths", [strs], _StorePathList.validate_python
        )

    # ── Build ─────────────────────────────────────────────────────

    async def build_paths_with_results(
        self,
        paths: list[StorePath | str],
    ) -> list[BuildResult]:
        self._check_active()
        strs = _to_strs(paths)
        return await manager_call(
            self._pool, "store", "build_paths_with_results", [strs], _BuildResultList.validate_python
        )

    async def read_derivation(
        self,
        drv_path: StorePath | str,
    ) -> Derivation:
        self._check_active()
        s = _to_str(drv_path)
        return await manager_call(
            self._pool, "store", "read_derivation", [s], _model_adapter(Derivation)
        )

    async def build_derivation(
        self,
        drv_path: StorePath | str,
        build_mode: nanopynix_store.BuildMode | int = nanopynix_store.BuildMode.Normal,
    ) -> BuildResult:
        self._check_active()
        s = _to_str(drv_path)
        mode = int(cast(Any, build_mode))
        return await manager_call(
            self._pool, "store", "build_derivation", [s, mode], _model_adapter(BuildResult)
        )

    # ── GC ────────────────────────────────────────────────────────

    async def add_temp_root(self, path: StorePath | str) -> None:
        self._check_active()
        s = _to_str(path)
        return await manager_call(self._pool, "store", "add_temp_root", [s], identity)

    # ── Fetchers ──────────────────────────────────────────────────

    async def fetch_from_url(self, url: str) -> Input:
        self._check_active()
        return await manager_call(self._pool, "store", "fetch_from_url", [url], _model_adapter(Input))

    async def fetch_from_attrs(
        self, attrs: dict[str, str | int | bool]
    ) -> Input:
        self._check_active()
        return await manager_call(
            self._pool, "store", "fetch_from_attrs", [attrs], _model_adapter(Input)
        )


# Backward-compatible alias
Store = StoreHandle
