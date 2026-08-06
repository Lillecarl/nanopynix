from __future__ import annotations

import sys
from typing import override

from clypi import Command, Positional, arg

from pynix._settings import store_option
from pynix._util import store_session


class Log(Command):
    """Show the build log for a store path"""

    path: Positional[str] = arg(help="Store path whose build log should be printed.")
    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        async with store_session(self.store) as (_nix, store):
            log = await store.get_build_log(self.path)

        if log is None:
            raise SystemExit(f"build log of '{self.path}' is not available")
        sys.stdout.write(log)
