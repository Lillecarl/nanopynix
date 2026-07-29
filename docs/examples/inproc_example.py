"""Evaluate Nix expressions and query the store via the in-process (L2) API.

Unlike ``Session`` (the subprocess/gRPC API), ``inproc.Session`` runs Nix
directly on a dedicated thread in *this* process — no worker subprocess, no
RPC serialization. Only one ``inproc.Session`` may be open per process, and
it owns at most one live ``EvalSession`` at a time. See the "Workers vs
in-process" guide for when to pick which.

Run with::

    python docs/examples/inproc_example.py
"""

# ruff: noqa: T201
# The printed output is the example. These are run by hand and by
# tests/nanopynix/test_examples.py; a logger would hide the very thing
# they exist to show.

from __future__ import annotations

import asyncio

from nanopynix import inproc


async def main() -> None:
    # load_config=False: Nix library initialization is process-global for
    # inproc.Session (see the architecture guide) — every inproc.Session in
    # this process, including in the test suite, must agree on settings.
    async with inproc.Session(load_config=False) as session, session.store() as store:
        # --- store queries: same shape as the worker-based Store facade ----

        all_paths = await store.query_all_valid_paths()
        assert len(all_paths) > 0
        sample = all_paths[0]
        print("sample path:", sample)

        info = await store.query_path_info(sample)
        assert info.path == str(sample)
        print("sample path nar_size:", info.nar_size)

        async with session.eval(store) as eval_:
            # --- to_python() converts a whole value tree --------------------
            #
            # One call flattens the tree using Nix's own toJSON rules. This
            # used to be spelled force(), which was the same operation under
            # a name that promised WHNF and delivered a deep conversion.

            v = await eval_.string('{ a = 1; b = "hello"; c = [ 1 2 3 ]; }')
            result = await v.to_python()
            assert result == {"a": 1, "b": "hello", "c": [1, 2, 3]}
            print("to_python() result:", result)

            # --- attr()/list_get() still navigate lazily before forcing -----

            v2 = await eval_.string("{ inner = { x = 42; }; z = 99; }")
            inner = v2.attr("inner")
            x = inner.attr("x")
            assert await x.as_int() == 42
            print("attr navigation: x =", await x.as_int())

            names = await v2.attr_names()
            assert set(names) == {"inner", "z"}
            print("attr_names:", names)

    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
