from __future__ import annotations

from typing import override

from libpynix import pos
from pynix import _impl
from pynix._settings import ConfiguredCommand, store_option


class PathInfo(ConfiguredCommand):
    """Show information about a store path"""

    path: str = pos(help="Store path to query (e.g. '/nix/store/hash-name').")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.path_info.run_path_info(self)
