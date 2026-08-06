from __future__ import annotations

from typing import TYPE_CHECKING, override

import structlog
from clypi import Positional, arg
from rich.text import Text

from nanopynix._typechecking import BEARTYPING
from pynix._settings import ConfiguredCommand, store_option
from pynix._util import error_console, print_json, store_session

if TYPE_CHECKING or BEARTYPING:
    import nanopynix

logger = structlog.get_logger(__name__)


class PathInfo(ConfiguredCommand):
    """Show information about a store path"""

    path: Positional[str] = arg(help="Store path to query (e.g. '/nix/store/hash-name').")
    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            try:
                info: nanopynix.PathInfo = await store.query_path_info(self.path)
            except Exception as exc:
                # stderr, and not stdout: the output of this command is JSON,
                # and `pynix path-info ... | jq` must not read this instead.
                #
                # `Text.from_ansi` keeps the colour of Nix, and interpolation
                # would lose it: rich reads a `[35;1m` in a markup string as a
                # tag, so the escape has to become a style before it arrives.
                # A `Text` is never markup, which is what makes this safe for
                # a message this command did not write.
                error_console.print("[red]Error:[/red]", Text.from_ansi(str(exc)))
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
