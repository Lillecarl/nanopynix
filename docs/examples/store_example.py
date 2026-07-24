"""Query a Nix store: path info, closures, and GC roots — all read-only.

Run with::

    python docs/examples/store_example.py
"""

# ruff: noqa: T201

from __future__ import annotations

import asyncio

from nanopynix import GcAction
from nanopynix.rpc import Session


async def main() -> None:
    async with Session() as session, session.store() as store:
        print("store dir:", await store.store_dir())
        print("store uri:", await store.uri())

        # --- listing and validating paths --------------------------------

        all_paths = await store.query_all_valid_paths()
        assert len(all_paths) > 0
        print("valid paths in store:", len(all_paths))

        sample = all_paths[0]
        assert await store.is_valid_path(sample)
        assert await store.is_valid_path(str(sample))  # plain str also accepted

        # --- path metadata --------------------------------------------------

        info = await store.query_path_info(sample)
        assert info.path == str(sample)
        print("sample path:", sample)
        print("sample path nar_size:", info.nar_size)

        # StorePath parses the hash/name out of the path string itself.
        print("sample path hash_part:", sample.hash_part)
        print("sample path name:", sample.name)
        print("sample path is_derivation:", sample.is_derivation)

        # --- closures ---------------------------------------------------

        closure = await store.compute_fs_closure(sample)
        assert sample in closure
        print("closure size:", len(closure))

        # --- garbage collector roots and a non-destructive GC query --------

        roots = await store.find_roots()
        print("GC roots:", len(roots))

        live = await store.collect_garbage(GcAction.RETURN_LIVE)
        assert len(live.paths) > 0
        print("live paths (RETURN_LIVE is a read-only query):", len(live.paths))

    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
