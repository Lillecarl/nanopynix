from __future__ import annotations

from typing import override

from clypi import Positional, arg

from pynix import _impl
from pynix._settings import ConfiguredCommand, store_option


class Log(ConfiguredCommand):
    """Show the build log for a store path"""

    path: Positional[str] = arg(help="Store path whose build log should be printed.")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.log.run_log(self)
