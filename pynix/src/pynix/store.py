from __future__ import annotations

import json
import sys
from typing import override

import structlog
from clypi import Command, Positional, arg
from nanopynix_proto.nix.store import (
    CollectGarbageRequest,
    EnsurePathRequest,
    FindRootsRequest,
    GcAction,
    OptimiseStoreRequest,
    QueryPathFromHashPartRequest,
    VerifyStoreRequest,
)

from pynix._util import prepare_sys_path

logger = structlog.get_logger(__name__)

_DEFAULT_STORE = "auto"


class PrintRoots(Command):
    """List the garbage collector roots"""

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, nix.store() as store:
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

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        action = GcAction.DELETE_DEAD if self.rip else GcAction.RETURN_DEAD
        async with nanopynix.Session() as nix, nix.store() as store:
            resp = await store.collect_garbage(CollectGarbageRequest(action=action))
            _print_json({"paths": list(resp.paths), "bytesFreed": resp.bytes_freed})


class PrintAlive(Command):
    """List live paths in the store (reachable from GC roots)"""

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, nix.store() as store:
            resp = await store.collect_garbage(CollectGarbageRequest(action=GcAction.RETURN_LIVE))
            _print_json({"paths": list(resp.paths)})


class Gc(Command):
    """Manage Nix store garbage collection"""

    subcommand: PrintRoots | PrintDead | PrintAlive


class PathFromHashPart(Command):
    """Resolve a store path from its hash prefix"""

    hash_part: Positional[str] = arg(help="Store path hash prefix to resolve.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, nix.store(self.store) as store:
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

        async with nanopynix.Session() as nix, nix.store(self.store) as store:
            await store.ensure_path(EnsurePathRequest(path=self.path))
            _print_json({"path": self.path, "valid": True})


class Optimise(Command):
    """Optimise store disk usage by hard-linking duplicate files"""

    store: str = arg(_DEFAULT_STORE, help="Store URI to optimise.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        async with nanopynix.Session() as nix, nix.store(self.store) as store:
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

        async with nanopynix.Session() as nix, nix.store(self.store) as store:
            resp = await store.verify_store(
                VerifyStoreRequest(check_contents=self.check_contents, repair=self.repair)
            )
            _print_json({"errors": resp.errors})


class Store(Command):
    """Manage the Nix store"""

    subcommand: Gc | PathFromHashPart | EnsurePath | Optimise | Verify



def _format_store_path(base_name: str) -> str:
    return f"/nix/store/{base_name}"


def _json_dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False)


def _print_json(obj: object) -> None:
    sys.stdout.write(_json_dumps(obj))
    sys.stdout.write("\n")
