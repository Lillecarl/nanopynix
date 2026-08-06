from __future__ import annotations

from typing import TYPE_CHECKING, override

import structlog
from clypi import Command, Positional, arg
from rich.console import Console

from nanopynix._typechecking import BEARTYPING
from pynix._settings import store_option
from pynix._util import print_json, store_session

if TYPE_CHECKING or BEARTYPING:
    import nanopynix

logger = structlog.get_logger(__name__)
console = Console()


class PathInfo(Command):
    """Show information about a store path"""

    path: Positional[str] = arg(help="Store path to query (e.g. '/nix/store/hash-name').")
    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            try:
                info: nanopynix.PathInfo = await store.query_path_info(self.path)
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
            print_json(result)
