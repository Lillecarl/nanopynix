from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

if TYPE_CHECKING:
    from collections.abc import Iterable

import structlog
from clypi import Command, Positional, arg

import nanopynix
from pynix._util import print_json, store_session

logger = structlog.get_logger(__name__)

_DEFAULT_STORE = "auto"


class PrintRoots(Command):
    """List the garbage collector roots"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            found_roots = await store.find_roots()
            roots = [
                {
                    "link": root.link,
                    "path": root.path,
                }
                for root in found_roots
            ]
            print_json({"roots": roots})


class PrintDead(Command):
    """List paths that would be removed by a garbage collection.
    Use --rip to actually delete them."""

    rip: bool = arg(
        False,
        help="Actually delete the dead store paths instead of just listing them.",
    )
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        action = nanopynix.GcAction.DELETE_DEAD if self.rip else nanopynix.GcAction.RETURN_DEAD
        async with store_session(self.store) as (_nix, store):
            result = await store.collect_garbage(action)
            print_json({"paths": list(result.paths), "bytesFreed": result.bytes_freed})


class PrintAlive(Command):
    """List live paths in the store (reachable from GC roots)"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            result = await store.collect_garbage(nanopynix.GcAction.RETURN_LIVE)
            print_json({"paths": list(result.paths)})


class Gc(Command):
    """Manage Nix store garbage collection"""

    subcommand: PrintRoots | PrintDead | PrintAlive


class Info(Command):
    """Show store metadata"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            uri = await store.uri()
            store_dir = await store.store_dir()
            dirs = await store.store_dirs()
            print_json({"uri": uri, "storeDir": store_dir, "dirs": _store_dirs_to_json(dirs)})


class Dirs(Command):
    """Show configured local store directories"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            dirs = await store.store_dirs()
            print_json(_store_dirs_to_json(dirs))


class IsValidPath(Command):
    """Check whether a store path is valid"""

    path: Positional[str] = arg(help="Store path to check.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            valid = await store.is_valid_path(self.path)
            print_json({"path": self.path, "valid": valid})


class FollowLinksToStorePath(Command):
    """Resolve symlinks to a store path"""

    path: Positional[str] = arg(help="Filesystem path to resolve.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            path = await store.follow_links_to_store_path(self.path)
            print_json({"path": path})


class ComputeFsClosure(Command):
    """Compute the filesystem closure of a store path"""

    path: Positional[str] = arg(help="Store path to query.")
    flip_direction: bool = arg(False, help="Compute the inverse closure.")
    include_outputs: bool = arg(False, help="Include derivation outputs.")
    include_derivers: bool = arg(False, help="Include derivers.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            paths = await store.compute_fs_closure(
                self.path,
                flip_direction=self.flip_direction,
                include_outputs=self.include_outputs,
                include_derivers=self.include_derivers,
            )
            _print_paths(paths)


class QueryMissing(Command):
    """Show which paths would need building, substituting, or are unknown"""

    paths: Positional[list[str]] = arg(help="Store paths to query.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        if not self.paths:
            raise SystemExit("query-missing requires at least one path")

        async with store_session(self.store) as (_nix, store):
            resp = await store.query_missing(self.paths)
            print_json(
                {
                    "willBuild": list(resp.will_build),
                    "willSubstitute": list(resp.will_substitute),
                    "unknown": list(resp.unknown),
                    "downloadSize": resp.download_size,
                    "narSize": resp.nar_size,
                },
            )


class QueryDerivationOutputs(Command):
    """Show the outputs of a derivation path"""

    path: Positional[str] = arg(help="Derivation path to query.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            paths = await store.query_derivation_outputs(self.path)
            _print_paths(paths)


class QueryValidDerivers(Command):
    """Show valid derivers for a store path"""

    path: Positional[str] = arg(help="Store path to query.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            paths = await store.query_valid_derivers(self.path)
            _print_paths(paths)


class ListValidPaths(Command):
    """List all valid paths in the store"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            paths = await store.query_all_valid_paths()
            _print_paths(paths)


class QueryReferrers(Command):
    """Show referrers of a store path"""

    path: Positional[str] = arg(help="Store path to query.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            paths = await store.query_referrers(self.path)
            _print_paths(paths)


class QuerySubstitutablePaths(Command):
    """Show which paths are substitutable"""

    paths: Positional[list[str]] = arg(help="Store paths to query.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        if not self.paths:
            raise SystemExit("query-substitutable-paths requires at least one path")

        async with store_session(self.store) as (_nix, store):
            paths = await store.query_substitutable_paths(self.paths)
            _print_paths(paths)


class AddTempRoot(Command):
    """Add a temporary GC root for this command's store session"""

    path: Positional[str] = arg(help="Store path to root temporarily.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            await store.add_temp_root(self.path)
            print_json({"path": self.path, "added": True})


class AddPermRoot(Command):
    """Add a permanent GC root symlink"""

    path: Positional[str] = arg(help="Store path to root.")
    gc_root: Positional[str] = arg(help="GC root symlink to create.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            gc_root = await store.add_perm_root(self.path, self.gc_root)
            print_json({"path": self.path, "gcRoot": gc_root})


class AddIndirectRoot(Command):
    """Register an indirect GC root"""

    path: Positional[str] = arg(help="GC root path to register.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            await store.add_indirect_root(self.path)
            print_json({"path": self.path, "added": True})


class PathFromHashPart(Command):
    """Resolve a store path from its hash prefix"""

    hash_part: Positional[str] = arg(help="Store path hash prefix to resolve.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            path = await store.query_path_from_hash_part(self.hash_part)
            print_json({"path": path})


class EnsurePath(Command):
    """Ensure a store path is valid, substituting it if available"""

    path: Positional[str] = arg(help="Store path to ensure.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            await store.ensure_path(self.path)
            print_json({"path": self.path, "valid": True})


class Cat(Command):
    """Print a file inside a local Nix store path"""

    path: Positional[str] = arg(help="File path to print.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            resolved = await _resolve_local_store_path(store, self.path)

        if not resolved.is_file():
            raise SystemExit(f"{self.path} is not a regular file")
        with resolved.open("rb") as f:
            sys.stdout.buffer.write(f.read())


class Ls(Command):
    """List files inside a local Nix store path"""

    path: Positional[str] = arg(help="File or directory path to list.")
    json: bool = arg(False, help="Print machine-readable JSON.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            resolved = await _resolve_local_store_path(store, self.path)

        if resolved.is_dir():
            entries = sorted(resolved.iterdir(), key=lambda path: path.name)
        elif resolved.exists():
            entries = [resolved]
        else:
            raise SystemExit(f"{self.path} does not exist")

        if self.json:
            print_json({"entries": [_directory_entry_to_json(entry) for entry in entries]})
            return
        for entry in entries:
            sys.stdout.write(entry.name)
            sys.stdout.write("\n")


class Add(Command):
    """Add a file or directory to a Nix store"""

    path: Positional[str] = arg(help="Filesystem path to add.")
    name: str | None = arg(None, short="n", help="Override the store path name component.")
    mode: str = arg("nar", help="Content-addressing method: nar, flat, or git.")
    hash_algo: str = arg("sha256", help="Hash algorithm to use.")
    dry_run: bool = arg(False, help="Compute the store path without adding the content.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        await _add_to_store(
            path=self.path,
            name=self.name,
            method=self.mode,
            hash_algo=self.hash_algo,
            dry_run=self.dry_run,
            store_uri=self.store,
        )


class AddFile(Command):
    """Add a single file to a Nix store"""

    path: Positional[str] = arg(help="Filesystem path to add.")
    name: str | None = arg(None, short="n", help="Override the store path name component.")
    hash_algo: str = arg("sha256", help="Hash algorithm to use.")
    dry_run: bool = arg(False, help="Compute the store path without adding the content.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        await _add_to_store(
            path=self.path,
            name=self.name,
            method="flat",
            hash_algo=self.hash_algo,
            dry_run=self.dry_run,
            store_uri=self.store,
        )


class AddPath(Command):
    """Add a path to a Nix store using NAR ingestion"""

    path: Positional[str] = arg(help="Filesystem path to add.")
    name: str | None = arg(None, short="n", help="Override the store path name component.")
    hash_algo: str = arg("sha256", help="Hash algorithm to use.")
    dry_run: bool = arg(False, help="Compute the store path without adding the content.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        await _add_to_store(
            path=self.path,
            name=self.name,
            method="nar",
            hash_algo=self.hash_algo,
            dry_run=self.dry_run,
            store_uri=self.store,
        )


class DiffClosures(Command):
    """Compare two filesystem closures"""

    before: Positional[str] = arg(help="Original store path.")
    after: Positional[str] = arg(help="New store path.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            before = await _closure_path_infos(store, self.before)
            after = await _closure_path_infos(store, self.after)

        before_paths = set(before)
        after_paths = set(after)
        added = sorted(after_paths - before_paths)
        removed = sorted(before_paths - after_paths)
        before_size = sum(before.values())
        after_size = sum(after.values())
        print_json(
            {
                "before": self.before,
                "after": self.after,
                "added": [{"path": path, "narSize": after[path]} for path in added],
                "removed": [{"path": path, "narSize": before[path]} for path in removed],
                "beforeNarSize": before_size,
                "afterNarSize": after_size,
                "narSizeDelta": after_size - before_size,
            },
        )


class Optimise(Command):
    """Optimise store disk usage by hard-linking duplicate files"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to optimise.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            await store.optimise_store()
            print_json({"optimised": True})


class Verify(Command):
    """Verify store integrity"""

    check_contents: bool = arg(False, help="Check path contents, not only metadata.")
    repair: bool = arg(False, help="Attempt repair while verifying.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to verify.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            errors = await store.verify_store(check_contents=self.check_contents, repair=self.repair)
            print_json({"errors": errors})


class Store(Command):
    """Manage the Nix store"""

    subcommand: (
        Gc
        | Info
        | Dirs
        | IsValidPath
        | FollowLinksToStorePath
        | ComputeFsClosure
        | QueryMissing
        | QueryDerivationOutputs
        | QueryValidDerivers
        | ListValidPaths
        | QueryReferrers
        | QuerySubstitutablePaths
        | AddTempRoot
        | AddPermRoot
        | AddIndirectRoot
        | PathFromHashPart
        | EnsurePath
        | Cat
        | Ls
        | Add
        | AddFile
        | AddPath
        | DiffClosures
        | Optimise
        | Verify
    )


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


async def _add_to_store(  # noqa: PLR0913 tracked complexity/arg-count debt, see TODO.md
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
