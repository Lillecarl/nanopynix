from __future__ import annotations

import json
import sys
from typing import override

import structlog
from clypi import Command, Positional, arg
from rich.console import Console

import nanopynix
from nanopynix_proto.nix.common import PathInfo as PathInfoProto  # noqa: TC002
from nanopynix_proto.nix.store import QueryPathInfoRequest

from pynix._util import forward_nix_logs, prepare_sys_path

logger = structlog.get_logger(__name__)
console = Console()

_DEFAULT_STORE = "auto"


class PathInfo(Command):
    """Show information about a store path"""

    path: Positional[str] = arg(help="Store path to query (e.g. '/nix/store/hash-name').")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            try:
                info: PathInfoProto = await store.query_path_info(QueryPathInfoRequest(path=self.path))
            except Exception as exc:
                console.print(f"[red]Error:[/red] {exc}")
                raise SystemExit(1) from exc
            result = {
                "path": info.path or self.path,
                "narHash": info.nar_hash,
                "narSize": info.nar_size,
                "references": list(info.references),
                "registrationTime": info.registration_time,
                "ultimate": info.ultimate,
                "ca": info.ca,
            }
            if info.deriver is not None:
                result["deriver"] = info.deriver
            sys.stdout.write(json.dumps(result, sort_keys=True, indent=2))
            sys.stdout.write("\n")
