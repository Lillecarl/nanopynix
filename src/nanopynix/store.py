"""Store facade — async Python wrapper over Nix store operations.

``StoreHandle`` is a context manager returned by ``Session.store()``.
It delegates every call to the session's ``_WorkerManager`` and carries
a ``_session_id`` that ``Eval`` checks at runtime.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from nanopynix.models import BuildResult, Capture, Derivation, LogEvent, MissingInfo, PathInfo, StorePath

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


def _raw_to_log_event(raw: dict) -> LogEvent:
    """Convert a raw worker log-event dict to a ``LogEvent`` model."""
    data: dict = {
        "request_id": raw.get("request_id", raw.get("id", 0)),
        "action": raw["action"],
        "args": raw["args"],
    }
    if raw.get("action") == "result" and len(raw.get("args", [])) > 1:
        data["result_type"] = raw["args"][1]
    return LogEvent.model_validate(data)


def _maybe_capture(result, capture_flag: bool):
    """If *capture_flag* is True, *result* is ``(value, raw_events)``.

    Returns ``Capture(value, logs=[...])`` or the plain value.
    """
    if capture_flag:
        value, raw_events = result
        logs = [_raw_to_log_event(e) for e in raw_events]
        return Capture(value=value, logs=logs)
    return result


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
        result = await self._pool.call("store", "get_uri", [], capture=capture)
        return _maybe_capture(result, capture)

    async def get_store_dir(self, *, capture: bool = False) -> Capture[str] | str:
        self._check_active()
        result = await self._pool.call("store", "get_store_dir", [], capture=capture)
        return _maybe_capture(result, capture)

    # ── StorePath parsing ─────────────────────────────────────────

    async def parse_store_path(self, path: str, *, capture: bool = False) -> Capture[StorePath] | StorePath:
        self._check_active()
        data = await self._pool.call("store", "parse_store_path", [path], capture=capture)
        result = StorePath.model_validate(data)
        return _maybe_capture(result, capture)

    async def is_valid_path(self, path: StorePath | str, *, capture: bool = False) -> Capture[bool] | bool:
        self._check_active()
        s = _to_str(path)
        result = await self._pool.call("store", "is_valid_path", [s], capture=capture)
        return _maybe_capture(result, capture)

    async def follow_links_to_store_path(self, path: str, *, capture: bool = False) -> Capture[StorePath] | StorePath:
        self._check_active()
        data = await self._pool.call("store", "follow_links_to_store_path", [path], capture=capture)
        result = StorePath.model_validate(data)
        return _maybe_capture(result, capture)

    # ── Path info ─────────────────────────────────────────────────

    async def query_path_info(self, path: StorePath | str, *, capture: bool = False) -> Capture[PathInfo] | PathInfo:
        self._check_active()
        s = _to_str(path)
        data = await self._pool.call("store", "query_path_info", [s], capture=capture)
        result = PathInfo.model_validate(data)
        return _maybe_capture(result, capture)

    async def query_path_from_hash_part(self, hash_part: str, *, capture: bool = False) -> Capture[StorePath] | StorePath:
        self._check_active()
        data = await self._pool.call("store", "query_path_from_hash_part", [hash_part], capture=capture)
        result = StorePath.model_validate(data)
        return _maybe_capture(result, capture)

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
        data = await self._pool.call("store", "compute_fs_closure",
            [s, flip, include_outputs, include_derivers], capture=capture)
        result = _StorePathList.validate_python(data)
        return _maybe_capture(result, capture)

    async def query_missing(self, paths: list[StorePath | str], *, capture: bool = False) -> Capture[MissingInfo] | MissingInfo:
        self._check_active()
        strs = _to_strs(paths)
        data = await self._pool.call("store", "query_missing", [strs], capture=capture)
        result = MissingInfo.model_validate(data)
        return _maybe_capture(result, capture)

    # ── Derivations ───────────────────────────────────────────────

    async def query_derivation_outputs(self, path: StorePath | str, *, capture: bool = False) -> Capture[list[StorePath]] | list[StorePath]:
        self._check_active()
        s = _to_str(path)
        data = await self._pool.call("store", "query_derivation_outputs", [s], capture=capture)
        result = _StorePathList.validate_python(data)
        return _maybe_capture(result, capture)

    async def query_valid_derivers(self, path: StorePath | str, *, capture: bool = False) -> Capture[list[StorePath]] | list[StorePath]:
        self._check_active()
        s = _to_str(path)
        data = await self._pool.call("store", "query_valid_derivers", [s], capture=capture)
        result = _StorePathList.validate_python(data)
        return _maybe_capture(result, capture)

    # ── Bulk queries ──────────────────────────────────────────────

    async def query_all_valid_paths(self, *, capture: bool = False) -> Capture[list[StorePath]] | list[StorePath]:
        self._check_active()
        data = await self._pool.call("store", "query_all_valid_paths", [], capture=capture)
        result = _StorePathList.validate_python(data)
        return _maybe_capture(result, capture)

    async def query_referrers(self, path: StorePath | str, *, capture: bool = False) -> Capture[list[StorePath]] | list[StorePath]:
        self._check_active()
        s = _to_str(path)
        data = await self._pool.call("store", "query_referrers", [s], capture=capture)
        result = _StorePathList.validate_python(data)
        return _maybe_capture(result, capture)

    async def query_substitutable_paths(self, paths: list[StorePath | str], *, capture: bool = False) -> Capture[list[StorePath]] | list[StorePath]:
        self._check_active()
        strs = _to_strs(paths)
        data = await self._pool.call("store", "query_substitutable_paths", [strs], capture=capture)
        result = _StorePathList.validate_python(data)
        return _maybe_capture(result, capture)

    # ── Build ─────────────────────────────────────────────────────

    async def build_paths_with_results(
        self, paths: list[StorePath | str],
        *, capture: bool = False,
    ) -> Capture[list[BuildResult]] | list[BuildResult]:
        self._check_active()
        strs = _to_strs(paths)
        data = await self._pool.call("store", "build_paths_with_results", [strs], capture=capture)
        result = _BuildResultList.validate_python(data)
        return _maybe_capture(result, capture)

    async def read_derivation(
        self, drv_path: StorePath | str,
        *, capture: bool = False,
    ) -> Capture[dict] | dict:
        self._check_active()
        s = _to_str(drv_path)
        result = await self._pool.call("store", "read_derivation", [s], capture=capture)
        return _maybe_capture(result, capture)

    async def build_derivation(
        self, drv_path: StorePath | str,
        build_mode: nanopynix_store.BuildMode | int = nanopynix_store.BuildMode.Normal,
        *, capture: bool = False,
    ) -> Capture[BuildResult] | BuildResult:
        self._check_active()
        s = _to_str(drv_path)
        mode = int(build_mode)
        data = await self._pool.call("store", "build_derivation", [s, mode], capture=capture)
        result = BuildResult.model_validate(data)
        return _maybe_capture(result, capture)

    # ── GC ────────────────────────────────────────────────────────

    async def add_temp_root(self, path: StorePath | str, *, capture: bool = False) -> Capture[None] | None:
        self._check_active()
        s = _to_str(path)
        result = await self._pool.call("store", "add_temp_root", [s], capture=capture)
        return _maybe_capture(result, capture)


# Backward-compatible alias
Store = StoreHandle
