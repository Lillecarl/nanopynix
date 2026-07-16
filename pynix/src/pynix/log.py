from __future__ import annotations

import sys
from typing import override

from clypi import Command, Positional, arg
from nanopynix_proto.nix.store import GetBuildLogRequest

import nanopynix
from pynix._util import forward_nix_logs, prepare_sys_path

_DEFAULT_STORE = "auto"


class Log(Command):
    """Show the build log for a store path"""

    path: Positional[str] = arg(help="Store path whose build log should be printed.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to query.")

    @override
    async def run(self) -> None:
        prepare_sys_path()

        async with nanopynix.Session() as nix, forward_nix_logs(nix), nix.store(self.store) as store:
            response = await store.rpc.get_build_log(GetBuildLogRequest(path=self.path))

        if response.log is None:
            raise SystemExit(f"build log of '{self.path}' is not available")
        sys.stdout.write(response.log)
