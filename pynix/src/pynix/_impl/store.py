"""The implementation of the ``pynix store`` command.

``pynix.store`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: clypi loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

import nanopynix
from nanopynix._typechecking import BEARTYPING
from pynix._util import print_json, store_session
from pynix.store import (
    Add,
    AddFile,
    AddIndirectRoot,
    AddPath,
    AddPermRoot,
    AddTempRoot,
    Cat,
    ComputeFsClosure,
    DiffClosures,
    Dirs,
    EnsurePath,
    FollowLinksToStorePath,
    Info,
    IsValidPath,
    ListValidPaths,
    Ls,
    Optimise,
    PathFromHashPart,
    PrintAlive,
    PrintDead,
    PrintRoots,
    QueryDerivationOutputs,
    QueryMissing,
    QueryReferrers,
    QuerySubstitutablePaths,
    QueryValidDerivers,
    Verify,
)

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Iterable
logger = structlog.get_logger("pynix.store")


def _print_paths(paths: Iterable[str]) -> None:
    print_json({"paths": list(paths)})


def _store_dirs_to_json(dirs: Any) -> dict[str, str | None]:
    return {
        "storeDir": dirs.store_dir,
        "uri": dirs.uri,
        "rootDir": dirs.root_dir,
        "stateDir": dirs.state_dir,
        "logDir": dirs.log_dir,
        "realStoreDir": dirs.real_store_dir,
        "buildDir": dirs.build_dir,
    }


async def _add_to_store(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    *,
    path: str,
    name: str | None,
    method: str,
    hash_algo: str,
    dry_run: bool,
    store_uri: str,
) -> None:
    async with store_session(store_uri) as (_nix, store):
        if dry_run:
            result_path = await store.compute_store_path(path, name=name, method=method, hash_algo=hash_algo)
        else:
            result_path = await store.add_to_store(path, name=name, method=method, hash_algo=hash_algo)
        print_json({"path": result_path})


async def _resolve_local_store_path(store: Any, path: str) -> Path:
    dirs = await store.store_dirs()
    store_dir = dirs.store_dir.rstrip("/")
    store_path, suffix = _split_store_path(path, store_dir)
    await store.query_path_info(store_path)

    if dirs.real_store_dir is None:
        raise SystemExit("store does not expose a local filesystem path")
    return Path(dirs.real_store_dir) / store_path.removeprefix(f"{store_dir}/") / suffix


def _split_store_path(path: str, store_dir: str) -> tuple[str, Path]:
    prefix = f"{store_dir}/"
    if not path.startswith(prefix):
        raise SystemExit(f"{path} is not inside {store_dir}")
    rest = path.removeprefix(prefix)
    base_name, separator, suffix = rest.partition("/")
    if not base_name:
        raise SystemExit(f"{path} is not a store path")
    suffix_path = Path(suffix) if separator else Path()
    if suffix_path.is_absolute() or ".." in suffix_path.parts:
        raise SystemExit(f"{path} escapes {store_dir}/{base_name}")
    return f"{store_dir}/{base_name}", suffix_path


def _directory_entry_to_json(path: Path) -> dict[str, object]:
    result: dict[str, object] = {"name": path.name}
    if path.is_symlink():
        result["type"] = "symlink"
        result["target"] = str(path.readlink())
    elif path.is_dir():
        result["type"] = "directory"
    elif path.is_file():
        result["type"] = "regular"
        result["size"] = path.stat().st_size
    else:
        result["type"] = "unknown"
    return result


async def _closure_path_infos(store: Any, path: str) -> dict[str, int]:
    paths = await store.compute_fs_closure(path)
    infos: dict[str, int] = {}
    for store_path in paths:
        path_string = store_path
        info = await store.query_path_info(path_string)
        infos[path_string] = info.nar_size
    return infos


async def run_print_roots(command: PrintRoots) -> None:
    """The body of :meth:`pynix.store.PrintRoots.run`."""
    async with store_session(command.store) as (_nix, store):
        found_roots = await store.find_roots()
        roots = [
            {
                "link": root.link,
                "path": root.path,
            }
            for root in found_roots
        ]
        print_json({"roots": roots})


async def run_print_dead(command: PrintDead) -> None:
    """The body of :meth:`pynix.store.PrintDead.run`."""
    action = nanopynix.GcAction.DELETE_DEAD if command.rip else nanopynix.GcAction.RETURN_DEAD
    async with store_session(command.store) as (_nix, store):
        result = await store.collect_garbage(action)
        print_json({"paths": list(result.paths), "bytesFreed": result.bytes_freed})


async def run_print_alive(command: PrintAlive) -> None:
    """The body of :meth:`pynix.store.PrintAlive.run`."""
    async with store_session(command.store) as (_nix, store):
        result = await store.collect_garbage(nanopynix.GcAction.RETURN_LIVE)
        print_json({"paths": list(result.paths)})


async def run_info(command: Info) -> None:
    """The body of :meth:`pynix.store.Info.run`."""
    async with store_session(command.store) as (_nix, store):
        uri = await store.uri()
        store_dir = await store.store_dir()
        dirs = await store.store_dirs()
        print_json({"uri": uri, "storeDir": store_dir, "dirs": _store_dirs_to_json(dirs)})


async def run_dirs(command: Dirs) -> None:
    """The body of :meth:`pynix.store.Dirs.run`."""
    async with store_session(command.store) as (_nix, store):
        dirs = await store.store_dirs()
        print_json(_store_dirs_to_json(dirs))


async def run_is_valid_path(command: IsValidPath) -> None:
    """The body of :meth:`pynix.store.IsValidPath.run`."""
    async with store_session(command.store) as (_nix, store):
        valid = await store.is_valid_path(command.path)
        print_json({"path": command.path, "valid": valid})


async def run_follow_links_to_store_path(command: FollowLinksToStorePath) -> None:
    """The body of :meth:`pynix.store.FollowLinksToStorePath.run`."""
    async with store_session(command.store) as (_nix, store):
        path = await store.follow_links_to_store_path(command.path)
        print_json({"path": path})


async def run_compute_fs_closure(command: ComputeFsClosure) -> None:
    """The body of :meth:`pynix.store.ComputeFsClosure.run`."""
    async with store_session(command.store) as (_nix, store):
        paths = await store.compute_fs_closure(
            command.path,
            flip_direction=command.flip_direction,
            include_outputs=command.include_outputs,
            include_derivers=command.include_derivers,
        )
        _print_paths(paths)


async def run_query_missing(command: QueryMissing) -> None:
    """The body of :meth:`pynix.store.QueryMissing.run`."""
    if not command.paths:
        raise SystemExit("query-missing requires at least one path")

    async with store_session(command.store) as (_nix, store):
        resp = await store.query_missing(command.paths)
        print_json(
            {
                "willBuild": list(resp.will_build),
                "willSubstitute": list(resp.will_substitute),
                "unknown": list(resp.unknown),
                "downloadSize": resp.download_size,
                "narSize": resp.nar_size,
            },
        )


async def run_query_derivation_outputs(command: QueryDerivationOutputs) -> None:
    """The body of :meth:`pynix.store.QueryDerivationOutputs.run`."""
    async with store_session(command.store) as (_nix, store):
        paths = await store.query_derivation_outputs(command.path)
        _print_paths(paths)


async def run_query_valid_derivers(command: QueryValidDerivers) -> None:
    """The body of :meth:`pynix.store.QueryValidDerivers.run`."""
    async with store_session(command.store) as (_nix, store):
        paths = await store.query_valid_derivers(command.path)
        _print_paths(paths)


async def run_list_valid_paths(command: ListValidPaths) -> None:
    """The body of :meth:`pynix.store.ListValidPaths.run`."""
    async with store_session(command.store) as (_nix, store):
        paths = await store.query_all_valid_paths()
        _print_paths(paths)


async def run_query_referrers(command: QueryReferrers) -> None:
    """The body of :meth:`pynix.store.QueryReferrers.run`."""
    async with store_session(command.store) as (_nix, store):
        paths = await store.query_referrers(command.path)
        _print_paths(paths)


async def run_query_substitutable_paths(command: QuerySubstitutablePaths) -> None:
    """The body of :meth:`pynix.store.QuerySubstitutablePaths.run`."""
    if not command.paths:
        raise SystemExit("query-substitutable-paths requires at least one path")

    async with store_session(command.store) as (_nix, store):
        paths = await store.query_substitutable_paths(command.paths)
        _print_paths(paths)


async def run_add_temp_root(command: AddTempRoot) -> None:
    """The body of :meth:`pynix.store.AddTempRoot.run`."""
    async with store_session(command.store) as (_nix, store):
        await store.add_temp_root(command.path)
        print_json({"path": command.path, "added": True})


async def run_add_perm_root(command: AddPermRoot) -> None:
    """The body of :meth:`pynix.store.AddPermRoot.run`."""
    async with store_session(command.store) as (_nix, store):
        gc_root = await store.add_perm_root(command.path, command.gc_root)
        print_json({"path": command.path, "gcRoot": gc_root})


async def run_add_indirect_root(command: AddIndirectRoot) -> None:
    """The body of :meth:`pynix.store.AddIndirectRoot.run`."""
    async with store_session(command.store) as (_nix, store):
        await store.add_indirect_root(command.path)
        print_json({"path": command.path, "added": True})


async def run_path_from_hash_part(command: PathFromHashPart) -> None:
    """The body of :meth:`pynix.store.PathFromHashPart.run`."""
    async with store_session(command.store) as (_nix, store):
        path = await store.query_path_from_hash_part(command.hash_part)
        print_json({"path": path})


async def run_ensure_path(command: EnsurePath) -> None:
    """The body of :meth:`pynix.store.EnsurePath.run`."""
    async with store_session(command.store) as (_nix, store):
        await store.ensure_path(command.path)
        print_json({"path": command.path, "valid": True})


async def run_cat(command: Cat) -> None:
    """The body of :meth:`pynix.store.Cat.run`."""
    async with store_session(command.store) as (_nix, store):
        resolved = await _resolve_local_store_path(store, command.path)

    if not resolved.is_file():
        raise SystemExit(f"{command.path} is not a regular file")
    with resolved.open("rb") as f:
        sys.stdout.buffer.write(f.read())


async def run_ls(command: Ls) -> None:
    """The body of :meth:`pynix.store.Ls.run`."""
    async with store_session(command.store) as (_nix, store):
        resolved = await _resolve_local_store_path(store, command.path)

    if resolved.is_dir():
        entries = sorted(resolved.iterdir(), key=lambda path: path.name)
    elif resolved.exists():
        entries = [resolved]
    else:
        raise SystemExit(f"{command.path} does not exist")

    if command.json:
        print_json({"entries": [_directory_entry_to_json(entry) for entry in entries]})
        return
    for entry in entries:
        sys.stdout.write(entry.name)
        sys.stdout.write("\n")


async def run_add(command: Add) -> None:
    """The body of :meth:`pynix.store.Add.run`."""
    await _add_to_store(
        path=command.path,
        name=command.name,
        method=command.mode,
        hash_algo=command.hash_algo,
        dry_run=command.dry_run,
        store_uri=command.store,
    )


async def run_add_file(command: AddFile) -> None:
    """The body of :meth:`pynix.store.AddFile.run`."""
    await _add_to_store(
        path=command.path,
        name=command.name,
        method="flat",
        hash_algo=command.hash_algo,
        dry_run=command.dry_run,
        store_uri=command.store,
    )


async def run_add_path(command: AddPath) -> None:
    """The body of :meth:`pynix.store.AddPath.run`."""
    await _add_to_store(
        path=command.path,
        name=command.name,
        method="nar",
        hash_algo=command.hash_algo,
        dry_run=command.dry_run,
        store_uri=command.store,
    )


async def run_diff_closures(command: DiffClosures) -> None:
    """The body of :meth:`pynix.store.DiffClosures.run`."""
    async with store_session(command.store) as (_nix, store):
        before = await _closure_path_infos(store, command.before)
        after = await _closure_path_infos(store, command.after)

    before_paths = set(before)
    after_paths = set(after)
    added = sorted(after_paths - before_paths)
    removed = sorted(before_paths - after_paths)
    before_size = sum(before.values())
    after_size = sum(after.values())
    print_json(
        {
            "before": command.before,
            "after": command.after,
            "added": [{"path": path, "narSize": after[path]} for path in added],
            "removed": [{"path": path, "narSize": before[path]} for path in removed],
            "beforeNarSize": before_size,
            "afterNarSize": after_size,
            "narSizeDelta": after_size - before_size,
        },
    )


async def run_optimise(command: Optimise) -> None:
    """The body of :meth:`pynix.store.Optimise.run`."""
    async with store_session(command.store) as (_nix, store):
        await store.optimise_store()
        print_json({"optimised": True})


async def run_verify(command: Verify) -> None:
    """The body of :meth:`pynix.store.Verify.run`."""
    async with store_session(command.store) as (_nix, store):
        errors = await store.verify_store(check_contents=command.check_contents, repair=command.repair)
        print_json({"errors": errors})
