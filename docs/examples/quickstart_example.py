"""The quickstart from ``README.md``, as a program that runs.

Run with::

    python docs/examples/quickstart_example.py

The ``region`` markers below are what ``README.md`` shows. See
``tests/meta/test_doc_snippets.py`` -- the page must match this file, and
``nanopynix/tests/test_examples.py`` runs the file.
"""

# ruff: noqa: T201
# The printed output is the example. These are run by hand and by
# nanopynix/tests/test_examples.py; a logger would hide the very thing
# they exist to show.

from __future__ import annotations

import asyncio

import nanopynix
from nanopynix import NixSettings


async def main() -> None:
    # region: quickstart
    async with (
        nanopynix.rpc.Session(settings=NixSettings(max_jobs=4)) as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        store_dir = await store.store_dir()
        root = await evaluator.string('{ name = "hello"; }')
        name = await root.attr("name").as_string()

    print(store_dir, name)
    # endregion: quickstart

    assert store_dir.endswith("/store"), store_dir
    assert name == "hello"

    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
