"""Search NixOS module options, using a cached, offline index."""

from __future__ import annotations

from typing import override

from libpynix import opt, pos
from pynix import _impl
from pynix._nix_options import attr_option, file_option, flake_option
from pynix._settings import ConfiguredCommand, store_option


class Search(ConfiguredCommand):
    """Search NixOS module options, using a cached, offline index."""

    query: str | None = pos(help="Search query. Omit to just (re)build the index.", default=None)

    file: str | None = file_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option(search="exact")

    options_attr: str | None = opt(
        None,
        help="Attribute path to the options tree, relative to the target. The default tries 'options'.",
    )

    lib_attr: str | None = opt(
        None,
        help="Attribute path to nixpkgs lib, relative to the target. The default tries 'lib', "
        "then the 'lib' of the package set that the target holds.",
    )

    update_index: bool = opt(False, help="Re-evaluate and rebuild the cached index instead of using it.")

    limit: int = opt(20, help="Maximum number of results to print.")

    json_output: bool = opt(False, short="j", help="Print results as JSON instead of a human-readable list.")

    tui: bool | None = opt(
        None,
        negatable=True,
        help="Browse the results in a full-screen interface. The default opens it "
        "for a person at a terminal who gave no query, and prints a list otherwise.",
    )

    store: str = store_option("Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        await _impl.search.run_search(self)
