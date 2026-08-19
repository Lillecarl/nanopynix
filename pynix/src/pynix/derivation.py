from __future__ import annotations

from typing import override

from pynix import _impl
from pynix._settings import ConfiguredCommand, PynixCommand, attr_option, file_option, flake_option, store_option


class Show(ConfiguredCommand):
    """Show the contents of a Nix derivation

    Examples:
      pynix derivation show --file default.nix --attr hello
      pynix derivation show --flake .#hello
      pynix derivation show --flake nixpkgs#python3Packages.requests"""

    file: str | None = file_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option()

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.derivation.run_show(self)


class Derivation(PynixCommand):
    """Inspect and manipulate Nix derivations"""

    subcommand: Show
