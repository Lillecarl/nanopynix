from __future__ import annotations

from typing import override

from clypi import arg

import nanopynix
from pynix._settings import PynixCommand
from pynix._util import print_json


class Show(PynixCommand):
    """Show Nix configuration settings"""

    setting: str | None = arg(None, help="Show only one setting.")

    @override
    async def run(self) -> None:
        nanopynix.init_libstore(load_config=True)
        settings = nanopynix.list_settings()
        if self.setting is not None:
            # Read one out of the whole registry rather than asking Nix for the
            # single name. A per-setting getter on the module reports the
            # globals of *this* process, which is the wrong process as soon as
            # a worker holds Nix, so nanopynix no longer offers one.
            print_json({self.setting: settings.get(self.setting)})
            return
        print_json(settings)


class Check(PynixCommand):
    """Check that Nix configuration can be loaded"""

    @override
    async def run(self) -> None:
        nanopynix.init_libstore(load_config=True)
        print_json({"ok": True})


class CurrentSystem(PynixCommand):
    """Show the effective system used by builtins.currentSystem"""

    @override
    async def run(self) -> None:
        nanopynix.init_libstore(load_config=True)
        print_json({"currentSystem": nanopynix.current_system()})


class Config(PynixCommand):
    """Inspect Nix configuration"""

    subcommand: Show | Check | CurrentSystem
