"""The smallest complete nanopynix program, shown on the documentation index.

Run with::

    python docs/examples/hello_example.py

The whole program is one region, because the point of the snippet on
``docs/nanopynix/index.md`` is the shape of a complete program.

**The ``if __name__`` guard is required, not a convention.** The rpc engine
starts its worker with the multiprocessing forkserver, and multiprocessing
refuses to start a child while the main module is still importing. Calling
``asyncio.run(main())`` at module level raises "An attempt has been made to
start a new process before the current process has finished its bootstrapping
phase". The index page published exactly that shape until #23.
"""

# ruff: noqa: T201
# The printed output is the example. These are run by hand and by
# nanopynix/tests/test_examples.py; a logger would hide the very thing
# they exist to show.

from __future__ import annotations

# region: hello
import asyncio

import nanopynix


async def main() -> None:
    async with (
        nanopynix.rpc.Session() as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        hello = await evaluator.string('"hello, world"')
        print(await hello.to_python())


if __name__ == "__main__":
    asyncio.run(main())
# endregion: hello
