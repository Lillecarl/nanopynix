from __future__ import annotations

import json
import sys
from typing import override

import structlog
from clypi import Command, arg
from nanopynix_proto.nix.store import CollectGarbageRequest, FindRootsRequest, GcAction

from pynix._util import prepare_sys_path

logger = structlog.get_logger(__name__)


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


class Store(Command):
    """Manage the Nix store"""

    subcommand: Gc


def _format_store_path(base_name: str) -> str:
    return f"/nix/store/{base_name}"


def _json_dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False)


def _print_json(obj: object) -> None:
    sys.stdout.write(_json_dumps(obj))
    sys.stdout.write("\n")
