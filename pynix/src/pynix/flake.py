from __future__ import annotations

# pyright: reportUnknownMemberType=false
# nanopynix / nanopynix_proto are C++ nanobind extensions without type stubs.
from typing import override

from pynix import _impl
from pynix._cli import opt, pos
from pynix._settings import ConfiguredCommand, PynixCommand, store_option


class Show(ConfiguredCommand):
    """Show the outputs provided by a flake"""

    flake_ref: str = pos(help="Flake reference (e.g. '.#' or 'nixpkgs#').")

    attrpath: str | None = opt(
        None,
        short="A",
        help="Dot-separated attribute path within the flake outputs to start from.",
    )

    store: str = store_option("Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        await _impl.flake.run_show(self)


class Metadata(ConfiguredCommand):
    """Show locked flake metadata"""

    flake_ref: str = pos(help="Flake reference (e.g. '.' or 'nixpkgs').")

    store: str = store_option("Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        await _impl.flake.run_metadata(self)


class Info(ConfiguredCommand):
    """Alias for flake metadata"""

    flake_ref: str = pos(help="Flake reference (e.g. '.' or 'nixpkgs').")

    store: str = store_option("Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        await _impl.flake.run_info(self)


class Flake(PynixCommand):
    """Inspect and manage Nix flakes"""

    subcommands = (Show, Metadata, Info)
