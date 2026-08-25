from __future__ import annotations

from typing import override

from libpynix import opt, pos
from pynix import _impl
from pynix._settings import ConfiguredCommand, store_option


class WhyDepends(ConfiguredCommand):
    """Show the chain of references that puts one store path in the closure of another"""

    package: str = pos(help="Store path whose closure to search (e.g. '/nix/store/hash-name').")

    dependency: str = pos(help="Store path to find in that closure.")

    precise: bool = opt(
        False,
        help="For each link of the chain, name the file that holds the reference. Needs a local store.",
    )

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.why_depends.run_why_depends(self)
