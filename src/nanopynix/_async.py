"""Async wrappers for nanopynix — await long-running Nix operations.

Usage:
    store = AsyncStore(nanopynix.open_store())
    results = await store.build_paths_with_results(paths)
"""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import nanopynix


class AsyncStore:
    """Async proxy for nanopynix.Store. Methods return awaitables."""

    def __init__(self, store: nanopynix.Store) -> None:
        self._store = store

    # ── Typed explicit wrappers for slow/long-running ops ──────────────

    async def build_paths(self, paths: list[nanopynix.StorePath]) -> None:
        return await asyncio.to_thread(self._store.build_paths, paths)

    async def build_paths_with_results(self, paths: list[nanopynix.StorePath]) -> list[nanopynix.BuildResult]:
        return await asyncio.to_thread(self._store.build_paths_with_results, paths)

    async def query_path_info(self, path: nanopynix.StorePath) -> nanopynix.PathInfo:
        return await asyncio.to_thread(self._store.query_path_info, path)

    async def compute_fs_closure(
        self,
        path: nanopynix.StorePath,
        flip_direction: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[str]:
        return await asyncio.to_thread(
            self._store.compute_fs_closure,
            path,
            flip_direction,
            include_outputs,
            include_derivers,
        )

    async def query_missing(self, paths: list[nanopynix.StorePath]) -> nanopynix.MissingInfo:
        return await asyncio.to_thread(self._store.query_missing, paths)

    async def query_all_valid_paths(self) -> list[str]:
        return await asyncio.to_thread(self._store.query_all_valid_paths)

    async def query_referrers(self, path: nanopynix.StorePath) -> list[str]:
        return await asyncio.to_thread(self._store.query_referrers, path)

    async def query_substitutable_paths(self, paths: list[nanopynix.StorePath]) -> list[str]:
        return await asyncio.to_thread(self._store.query_substitutable_paths, paths)

    # ── Fast ops — explicit passthrough for type safety ────────────────

    def get_store_dir(self) -> str:
        return self._store.get_store_dir()

    def get_uri(self) -> str:
        return self._store.get_uri()

    def is_valid_path(self, path: nanopynix.StorePath) -> bool:
        return self._store.is_valid_path(path)

    def parse_store_path(self, path: str) -> nanopynix.StorePath:
        return self._store.parse_store_path(path)

    def follow_links_to_store_path(self, path: str) -> nanopynix.StorePath:
        return self._store.follow_links_to_store_path(path)

    def query_path_from_hash_part(self, hash_part: str) -> nanopynix.StorePath | None:
        return self._store.query_path_from_hash_part(hash_part)

    def query_derivation_outputs(self, path: nanopynix.StorePath) -> list[str]:
        return self._store.query_derivation_outputs(path)

    def query_valid_derivers(self, path: nanopynix.StorePath) -> list[str]:
        return self._store.query_valid_derivers(path)

    def add_temp_root(self, path: nanopynix.StorePath) -> None:
        return self._store.add_temp_root(path)

    # ── Fallback — asyncifies any unknown method ───────────────────────

    def __getattr__(self, name: str):
        attr = getattr(self._store, name)
        if not callable(attr) or name.startswith("_"):
            return attr

        @functools.wraps(attr)
        async def _wrapper(*args, **kwargs):
            return await asyncio.to_thread(attr, *args, **kwargs)

        # Cache on the instance so __getattr__ only fires once per method
        object.__setattr__(self, name, _wrapper)
        return _wrapper

    def __dir__(self):
        return dir(self._store)


class AsyncEvalState:
    """Async proxy for nanopynix.EvalState."""

    def __init__(self, eval_state: nanopynix.EvalState) -> None:
        self._es = eval_state

    async def eval_string(self, expr: str, path: str = "<string>") -> nanopynix.Value:
        return await asyncio.to_thread(self._es.eval_string, expr, path)

    def alloc_value(self) -> nanopynix.Value:
        return self._es.alloc_value()

    def __getattr__(self, name: str):
        attr = getattr(self._es, name)
        if not callable(attr) or name.startswith("_"):
            return attr

        @functools.wraps(attr)
        async def _wrapper(*args, **kwargs):
            return await asyncio.to_thread(attr, *args, **kwargs)

        object.__setattr__(self, name, _wrapper)
        return _wrapper

    def __dir__(self):
        return dir(self._es)


# ── Module-level async free functions ─────────────────────────────────


async def eval_file(state: nanopynix.EvalState, path: str) -> nanopynix.Value:
    import nanopynix

    return await asyncio.to_thread(nanopynix.eval_file, state, path)


async def lock_flake(
    state: nanopynix.EvalState,
    flake_ref: nanopynix.FlakeRef,
    update_lock_file: bool = True,
    write_lock_file: bool = True,
) -> nanopynix.LockedFlake:
    import nanopynix

    return await asyncio.to_thread(
        nanopynix.lock_flake,
        state,
        flake_ref,
        update_lock_file,
        write_lock_file,
    )


async def get_flake(
    state: nanopynix.EvalState,
    flake_ref: nanopynix.FlakeRef,
    use_registries: bool = True,
) -> nanopynix.FlakeRef:
    import nanopynix

    return await asyncio.to_thread(
        nanopynix.get_flake,
        state,
        flake_ref,
        use_registries,
    )
