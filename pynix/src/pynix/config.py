from __future__ import annotations

from typing import override

from libpynix import opt
from pynix import _impl
from pynix._settings import PynixCommand


class Show(PynixCommand):
    """Show Nix configuration settings"""

    setting: str | None = opt(None, help="Show only one setting.")

    @override
    async def run(self) -> None:
        await _impl.config.run_show(self)


class Check(PynixCommand):
    """Check that Nix configuration can be loaded"""

    @override
    async def run(self) -> None:
        await _impl.config.run_check(self)


class CurrentSystem(PynixCommand):
    """Show the effective system used by builtins.currentSystem"""

    @override
    async def run(self) -> None:
        await _impl.config.run_current_system(self)


class Config(PynixCommand):
    """Inspect Nix configuration"""

    subcommands = (Show, Check, CurrentSystem)
