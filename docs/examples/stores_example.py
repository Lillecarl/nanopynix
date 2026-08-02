"""Open a store from a typed model instead of a URI string.

Run with::

    python docs/examples/stores_example.py

Its own file rather than a region of ``store_example.py``, because that one is
given an isolated seeded store by ``tests/nanopynix/test_examples.py``. This
example brings its own store, so it needs none of that.
"""

# ruff: noqa: T201
# The printed output is the example. These are run by hand and by
# tests/nanopynix/test_examples.py; a logger would hide the very thing
# they exist to show.

from __future__ import annotations

import asyncio
import tempfile

import nanopynix
from nanopynix import stores


async def main() -> None:
    with tempfile.TemporaryDirectory() as root:
        # region: model
        async with (
            nanopynix.rpc.Session() as nix,
            nix.store(stores.Local(root=root, require_sigs=False)) as store,
        ):
            print(await store.uri())
        # endregion: model

        # The model renders the URI, so the two spellings open the same store.
        assert stores.Local(root=root, require_sigs=False).uri() == f"local://?require-sigs=false&root={root}"

    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
