"""Store facade — async Python wrapper over Nix store operations.

``StoreHandle`` is a context manager returned by ``Session.store()``.
It delegates every call to the session's ``_WorkerManager`` and carries
a ``_session_id`` that ``Eval`` checks at runtime.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Callable, TypeVar, cast

from pydantic import BaseModel, TypeAdapter

from nanopynix._rpc import identity, manager_call, raw_to_log_event
from nanopynix.models import BuildResult, Capture, Derivation, Input, LogEvent, MissingInfo, PathInfo, StorePath

import nanopynix_store  # BuildMode enum

if TYPE_CHECKING:
    from nanopynix._pool import _WorkerManager

_StorePathList = TypeAdapter(list[StorePath])
_BuildResultList = TypeAdapter(list[BuildResult])
T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)


def _to_str(path: StorePath | str) -> str:
    """Coerce a StorePath or str to a store-path string."""
    return path.to_string if isinstance(path, StorePath) else path


def _to_strs(paths: list[StorePath | str]) -> list[str]:
    """Coerce a list of StorePath|str to a list of store-path strings."""
    return [p.to_string if isinstance(p, StorePath) else p for p in paths]


_raw_to_log_event = raw_to_log_event


def _model_adapter(model: type[M]) -> Callable[[Any], M]:
    return model.model_validate


class StoreHandle:
    """Async wrapper around a Nix store — context manager for lifecycle.

    Created via ``Session.store(uri="daemon")``.  Carries a ``_session_id``
    that ``Eval`` checks at runtime to prevent cross-session usage.

    Usage::

        async with session.store() as store:
            info = await store.query_path_info(sp, capture=True)
            print(info.value.nar_hash)
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

    async def get_uri(self, *, capture: bool = False) -> Capture[str] | str:
        self._check_active()
        return await manager_call(self._pool, "store", "get_uri", [], str, capture=capture)

    async def get_store_dir(self, *, capture: bool = False) -> Capture[str] | str:
        self._check_active()
        return await manager_call(self._pool, "store", "get_store_dir", [], str, capture=capture)

    # ── StorePath parsing ─────────────────────────────────────────

    async def parse_store_path(self, path: str, *, capture: bool = False) -> Capture[StorePath] | StorePath:
        self._check_active()
        return await manager_call(
            self._pool, "store", "parse_store_path", [path], _model_adapter(StorePath), capture=capture
        )

    async def is_valid_path(self, path: StorePath | str, *, capture: bool = False) -> Capture[bool] | bool:
        self._check_active()
        s = _to_str(path)
        return await manager_call(self._pool, "store", "is_valid_path", [s], bool, capture=capture)

    async def follow_links_to_store_path(self, path: str, *, capture: bool = False) -> Capture[StorePath] | StorePath:
        self._check_active()
        return await manager_call(
            self._pool, "store", "follow_links_to_store_path", [path], _model_adapter(StorePath), capture=capture
        )

    # ── Path info ─────────────────────────────────────────────────

    async def query_path_info(self, path: StorePath | str, *, capture: bool = False) -> Capture[PathInfo] | PathInfo:
        self._check_active()
        s = _to_str(path)
        return await manager_call(
            self._pool, "store", "query_path_info", [s], _model_adapter(PathInfo), capture=capture
        )

    async def query_path_from_hash_part(
        self, hash_part: str, *, capture: bool = False
    ) -> Capture[StorePath | None] | StorePath | None:
        self._check_active()

        def adapt(value):
            return None if value is None else StorePath.model_validate(value)

        return await manager_call(self._pool, "store", "query_path_from_hash_part", [hash_part], adapt, capture=capture)

    # ── Closures ──────────────────────────────────────────────────

    async def compute_fs_closure(
        self,
        path: StorePath | str,
        flip: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
        *,
        capture: bool = False,
    ) -> Capture[list[StorePath]] | list[StorePath]:
        self._check_active()
        s = _to_str(path)
        return await manager_call(
            self._pool,
            "store",
            "compute_fs_closure",
            [s, flip, include_outputs, include_derivers],
            _StorePathList.validate_python,
            capture=capture,
        )

    async def query_missing(
        self, paths: list[StorePath | str], *, capture: bool = False
    ) -> Capture[MissingInfo] | MissingInfo:
        self._check_active()
        strs = _to_strs(paths)
        return await manager_call(
            self._pool, "store", "query_missing", [strs], _model_adapter(MissingInfo), capture=capture
        )

    # ── Derivations ───────────────────────────────────────────────

    async def query_derivation_outputs(
        self, path: StorePath | str, *, capture: bool = False
    ) -> Capture[list[StorePath]] | list[StorePath]:
        self._check_active()
        s = _to_str(path)
        return await manager_call(
            self._pool, "store", "query_derivation_outputs", [s], _StorePathList.validate_python, capture=capture
        )

    async def query_valid_derivers(
        self, path: StorePath | str, *, capture: bool = False
    ) -> Capture[list[StorePath]] | list[StorePath]:
        self._check_active()
        s = _to_str(path)
        return await manager_call(
            self._pool, "store", "query_valid_derivers", [s], _StorePathList.validate_python, capture=capture
        )

    # ── Bulk queries ──────────────────────────────────────────────

    async def query_all_valid_paths(self, *, capture: bool = False) -> Capture[list[StorePath]] | list[StorePath]:
        self._check_active()
        return await manager_call(
            self._pool, "store", "query_all_valid_paths", [], _StorePathList.validate_python, capture=capture
        )

    async def query_referrers(
        self, path: StorePath | str, *, capture: bool = False
    ) -> Capture[list[StorePath]] | list[StorePath]:
        self._check_active()
        s = _to_str(path)
        return await manager_call(
            self._pool, "store", "query_referrers", [s], _StorePathList.validate_python, capture=capture
        )

    async def query_substitutable_paths(
        self, paths: list[StorePath | str], *, capture: bool = False
    ) -> Capture[list[StorePath]] | list[StorePath]:
        self._check_active()
        strs = _to_strs(paths)
        return await manager_call(
            self._pool, "store", "query_substitutable_paths", [strs], _StorePathList.validate_python, capture=capture
        )

    # ── Build ─────────────────────────────────────────────────────

    async def build_paths_with_results(
        self,
        paths: list[StorePath | str],
        *,
        capture: bool = False,
    ) -> Capture[list[BuildResult]] | list[BuildResult]:
        self._check_active()
        strs = _to_strs(paths)
        return await manager_call(
            self._pool, "store", "build_paths_with_results", [strs], _BuildResultList.validate_python, capture=capture
        )

    async def read_derivation(
        self,
        drv_path: StorePath | str,
        *,
        capture: bool = False,
    ) -> Capture[Derivation] | Derivation:
        self._check_active()
        s = _to_str(drv_path)
        return await manager_call(
            self._pool, "store", "read_derivation", [s], _model_adapter(Derivation), capture=capture
        )

    async def build_derivation(
        self,
        drv_path: StorePath | str,
        build_mode: nanopynix_store.BuildMode | int = nanopynix_store.BuildMode.Normal,
        *,
        capture: bool = False,
    ) -> Capture[BuildResult] | BuildResult:
        self._check_active()
        s = _to_str(drv_path)
        mode = int(cast(Any, build_mode))
        return await manager_call(
            self._pool, "store", "build_derivation", [s, mode], _model_adapter(BuildResult), capture=capture
        )

    # ── GC ────────────────────────────────────────────────────────

    async def add_temp_root(self, path: StorePath | str, *, capture: bool = False) -> Capture[None] | None:
        self._check_active()
        s = _to_str(path)
        return await manager_call(self._pool, "store", "add_temp_root", [s], identity, capture=capture)

    # ── Fetchers ──────────────────────────────────────────────────

    async def fetch_from_url(self, url: str, *, capture: bool = False) -> Capture[Input] | Input:
        self._check_active()
        return await manager_call(self._pool, "store", "fetch_from_url", [url], _model_adapter(Input), capture=capture)

    async def fetch_from_attrs(
        self, attrs: dict[str, str | int | bool], *, capture: bool = False
    ) -> Capture[Input] | Input:
        self._check_active()
        return await manager_call(
            self._pool, "store", "fetch_from_attrs", [attrs], _model_adapter(Input), capture=capture
        )


# Backward-compatible alias
Store = StoreHandle
