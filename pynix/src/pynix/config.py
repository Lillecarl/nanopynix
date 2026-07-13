from __future__ import annotations

import json
import sys
from typing import override

from clypi import Command, arg

from pynix._util import prepare_sys_path


class Show(Command):
    """Show Nix configuration settings"""

    setting: str | None = arg(None, help="Show only one setting.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        nanopynix.init_libstore(load_config=True)
        if self.setting is not None:
            _print_json({self.setting: nanopynix.get_setting(self.setting)})
            return
        _print_json(nanopynix.list_settings())


class Check(Command):
    """Check that Nix configuration can be loaded"""

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        nanopynix.init_libstore(load_config=True)
        _print_json({"ok": True})


class CurrentSystem(Command):
    """Show the effective system used by builtins.currentSystem"""

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        nanopynix.init_libstore(load_config=True)
        _print_json({"currentSystem": nanopynix.current_system()})


class Config(Command):
    """Inspect Nix configuration"""

    subcommand: Show | Check | CurrentSystem


def _print_json(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False))
    sys.stdout.write("\n")
