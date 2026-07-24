from __future__ import annotations

from typing import override

from clypi import Command, arg

import nanopynix
from pynix._util import print_json


class Show(Command):
    """Show Nix configuration settings"""

    setting: str | None = arg(None, help="Show only one setting.")

    @override
    async def run(self) -> None:
        nanopynix.init_libstore(load_config=True)
        if self.setting is not None:
            print_json({self.setting: nanopynix.get_setting(self.setting)})
            return
        print_json(nanopynix.list_settings())


class Check(Command):
    """Check that Nix configuration can be loaded"""

    @override
    async def run(self) -> None:
        nanopynix.init_libstore(load_config=True)
        print_json({"ok": True})


class CurrentSystem(Command):
    """Show the effective system used by builtins.currentSystem"""

    @override
    async def run(self) -> None:
        nanopynix.init_libstore(load_config=True)
        print_json({"currentSystem": nanopynix.current_system()})


class Config(Command):
    """Inspect Nix configuration"""

    subcommand: Show | Check | CurrentSystem
