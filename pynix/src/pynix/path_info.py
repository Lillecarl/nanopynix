from __future__ import annotations

import json
from typing import override

import structlog
from clypi import Command, Positional, arg
from rich.console import Console

from pynix._util import prepare_sys_path

logger = structlog.get_logger(__name__)
console = Console()


class PathInfo(Command):
    """Show information about a store path"""

    path: Positional[str] = arg(help="Store path to query (e.g. '/nix/store/hash-name').")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        from nanopynix_proto.nix.common import PathInfo as PathInfoProto  # noqa: TC002
        from nanopynix_proto.nix.store import QueryPathInfoRequest

        import nanopynix

        async with nanopynix.Session() as nix, nix.store() as store:
            try:
                info: PathInfoProto = await store.query_path_info(QueryPathInfoRequest(path=self.path))
            except Exception as exc:
                console.print(f"[red]Error:[/red] {exc}")
                raise SystemExit(1) from exc
            result = {
                "path": _store_path_str(info.path) if info.path else self.path,
                "narHash": info.nar_hash,
                "narSize": info.nar_size,
                "references": [_store_path_str(r) for r in info.references],
                "registrationTime": info.registration_time,
                "ultimate": info.ultimate,
                "ca": info.ca,
            }
            if info.deriver is not None:
                result["deriver"] = _store_path_str(info.deriver)
            console.print(json.dumps(result, sort_keys=True, indent=2))


def _store_path_str(sp) -> str:
    return f"/nix/store/{sp.base_name}"
