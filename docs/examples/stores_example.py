"""Open a store from a typed model instead of a URI string.

Run with::

    python docs/examples/stores_example.py

Its own file rather than a region of ``store_example.py``, because that one is
given an isolated seeded store by ``nanopynix/tests/test_examples.py``. This
example brings its own store, so it needs none of that.
"""

# ruff: noqa: T201
# The printed output is the example. These are run by hand and by
# nanopynix/tests/test_examples.py; a logger would hide the very thing
# they exist to show.

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import nanopynix
from nanopynix import stores


async def main(root: str) -> None:
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
    with tempfile.TemporaryDirectory() as raw_root:
        # Nix refuses a store whose path or parent directory is a symlink. On
        # macOS `/tmp` is a symlink to `/private/tmp`, and the temporary
        # directory sits under it, so give Nix the resolved path. This costs
        # nothing on Linux, where the two spellings are already the same.
        #
        # The resolution happens here, and not in `main`, because it reads the
        # file system. A coroutine that makes a blocking call of that kind
        # stops the event loop, and `__main__` is synchronous.
        asyncio.run(main(str(Path(raw_root).resolve())))
