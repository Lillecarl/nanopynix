from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from typing import Any, override

import structlog
from clypi import Command, Positional, arg
from nanopynix_proto.nix.store import (
    AddIndirectRootRequest,
    AddPermRootRequest,
    AddTempRootRequest,
    CollectGarbageRequest,
    ComputeFsClosureRequest,
    EnsurePathRequest,
    FindRootsRequest,
    FollowLinksToStorePathRequest,
    GcAction,
    GetStoreDirRequest,
    GetUriRequest,
    IsValidPathRequest,
    OptimiseStoreRequest,
    QueryAllValidPathsRequest,
    QueryDerivationOutputsRequest,
    QueryMissingRequest,
    QueryPathFromHashPartRequest,
    QueryReferrersRequest,
    QuerySubstitutablePathsRequest,
    QueryValidDeriversRequest,
    VerifyStoreRequest,
)

from pynix._util import forward_nix_logs, prepare_sys_path

logger = structlog.get_logger(__name__)

_DEFAULT_STORE = "auto"


class PrintRoots(Command):
    """List the garbage collector roots"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.find_roots(FindRootsRequest())
            roots = []
            for root in resp.roots:
                path = root.path
                if path is None:
                    continue
                roots.append(
                    {
                        "link": root.link,
                        "path": _format_store_path(path.base_name),
                    }
                )
            _print_json({"roots": roots})


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
        prepare_sys_path()
        import nanopynix

        action = GcAction.DELETE_DEAD if self.rip else GcAction.RETURN_DEAD
        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.collect_garbage(CollectGarbageRequest(action=action))
            _print_json({"paths": list(resp.paths), "bytesFreed": resp.bytes_freed})


class PrintAlive(Command):
    """List live paths in the store (reachable from GC roots)"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.collect_garbage(CollectGarbageRequest(action=GcAction.RETURN_LIVE))
            _print_json({"paths": list(resp.paths)})


class Gc(Command):
    """Manage Nix store garbage collection"""

    subcommand: PrintRoots | PrintDead | PrintAlive


class Info(Command):
    """Show store metadata"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            uri = await store.get_uri(GetUriRequest())
            store_dir = await store.get_store_dir(GetStoreDirRequest())
            _print_json({"uri": uri.uri, "storeDir": store_dir.dir})


class IsValidPath(Command):
    """Check whether a store path is valid"""

    path: Positional[str] = arg(help="Store path to check.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.is_valid_path(IsValidPathRequest(path=self.path))
            _print_json({"path": self.path, "valid": resp.valid})


class FollowLinksToStorePath(Command):
    """Resolve symlinks to a store path"""

    path: Positional[str] = arg(help="Filesystem path to resolve.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            path = await store.follow_links_to_store_path(FollowLinksToStorePathRequest(path=self.path))
            _print_json({"path": _format_store_path(path.base_name)})


class ComputeFsClosure(Command):
    """Compute the filesystem closure of a store path"""

    path: Positional[str] = arg(help="Store path to query.")
    flip_direction: bool = arg(False, help="Compute the inverse closure.")
    include_outputs: bool = arg(False, help="Include derivation outputs.")
    include_derivers: bool = arg(False, help="Include derivers.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.compute_fs_closure(
                ComputeFsClosureRequest(
                    path=self.path,
                    flip_direction=self.flip_direction,
                    include_outputs=self.include_outputs,
                    include_derivers=self.include_derivers,
                )
            )
            _print_paths(resp.paths)


class QueryMissing(Command):
    """Show which paths would need building, substituting, or are unknown"""

    paths: Positional[list[str]] = arg(help="Store paths to query.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        if not self.paths:
            raise SystemExit("query-missing requires at least one path")

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.query_missing(QueryMissingRequest(paths=self.paths))
            _print_json(
                {
                    "willBuild": [_format_store_path(path.base_name) for path in resp.will_build],
                    "willSubstitute": [_format_store_path(path.base_name) for path in resp.will_substitute],
                    "unknown": [_format_store_path(path.base_name) for path in resp.unknown],
                    "downloadSize": resp.download_size,
                    "narSize": resp.nar_size,
                }
            )


class QueryDerivationOutputs(Command):
    """Show the outputs of a derivation path"""

    path: Positional[str] = arg(help="Derivation path to query.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.query_derivation_outputs(QueryDerivationOutputsRequest(path=self.path))
            _print_paths(resp.paths)


class QueryValidDerivers(Command):
    """Show valid derivers for a store path"""

    path: Positional[str] = arg(help="Store path to query.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.query_valid_derivers(QueryValidDeriversRequest(path=self.path))
            _print_paths(resp.paths)


class ListValidPaths(Command):
    """List all valid paths in the store"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.query_all_valid_paths(QueryAllValidPathsRequest())
            _print_paths(resp.paths)


class QueryReferrers(Command):
    """Show referrers of a store path"""

    path: Positional[str] = arg(help="Store path to query.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.query_referrers(QueryReferrersRequest(path=self.path))
            _print_paths(resp.paths)


class QuerySubstitutablePaths(Command):
    """Show which paths are substitutable"""

    paths: Positional[list[str]] = arg(help="Store paths to query.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        if not self.paths:
            raise SystemExit("query-substitutable-paths requires at least one path")

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.query_substitutable_paths(QuerySubstitutablePathsRequest(paths=self.paths))
            _print_paths(resp.paths)


class AddTempRoot(Command):
    """Add a temporary GC root for this command's store session"""

    path: Positional[str] = arg(help="Store path to root temporarily.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            await store.add_temp_root(AddTempRootRequest(path=self.path))
            _print_json({"path": self.path, "added": True})


class AddPermRoot(Command):
    """Add a permanent GC root symlink"""

    path: Positional[str] = arg(help="Store path to root.")
    gc_root: Positional[str] = arg(help="GC root symlink to create.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.add_perm_root(AddPermRootRequest(store_path=self.path, gc_root=self.gc_root))
            _print_json({"path": self.path, "gcRoot": resp.path})


class AddIndirectRoot(Command):
    """Register an indirect GC root"""

    path: Positional[str] = arg(help="GC root path to register.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            await store.add_indirect_root(AddIndirectRootRequest(path=self.path))
            _print_json({"path": self.path, "added": True})


class PathFromHashPart(Command):
    """Resolve a store path from its hash prefix"""

    hash_part: Positional[str] = arg(help="Store path hash prefix to resolve.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part=self.hash_part))
            path = _format_store_path(resp.path.base_name) if resp.path is not None else None
            _print_json({"path": path})


class EnsurePath(Command):
    """Ensure a store path is valid, substituting it if available"""

    path: Positional[str] = arg(help="Store path to ensure.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to use.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            await store.ensure_path(EnsurePathRequest(path=self.path))
            _print_json({"path": self.path, "valid": True})


class Optimise(Command):
    """Optimise store disk usage by hard-linking duplicate files"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to optimise.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            await store.optimise_store(OptimiseStoreRequest())
            _print_json({"optimised": True})


class Verify(Command):
    """Verify store integrity"""

    check_contents: bool = arg(False, help="Check path contents, not only metadata.")
    repair: bool = arg(False, help="Attempt repair while verifying.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to verify.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            resp = await store.verify_store(
                VerifyStoreRequest(check_contents=self.check_contents, repair=self.repair)
            )
            _print_json({"errors": resp.errors})


class Store(Command):
    """Manage the Nix store"""

    subcommand: (
        Gc
        | Info
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
        | Optimise
        | Verify
    )



def _format_store_path(base_name: str) -> str:
    return f"/nix/store/{base_name}"


def _json_dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False)


def _print_json(obj: object) -> None:
    sys.stdout.write(_json_dumps(obj))
    sys.stdout.write("\n")


def _print_paths(paths: Iterable[Any]) -> None:
    _print_json({"paths": [_format_store_path(path.base_name) for path in paths]})
