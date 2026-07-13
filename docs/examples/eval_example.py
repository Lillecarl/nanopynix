"""Evaluate Nix expressions and navigate results.

Run with::

    python docs/examples/eval_example.py
"""

# ruff: noqa: T201

from __future__ import annotations

import asyncio

from nanopynix import NixType, Session


async def main() -> None:
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        # --- eval.string: inline Nix expressions -------------------------------

        v = await eval.string('{ a = 1; b = "hello"; c = true; d = [ 10 20 ]; }')
        assert await v.get_type() == NixType.ATTRS

        # force_deep converts the entire value tree to nested Python dicts/lists.
        result = await v.force_deep()
        assert result == {"a": 1, "b": "hello", "c": True, "d": [10, 20]}
        print("force_deep:", result)

        # --- attr navigation without full deep conversion ---------------------

        v2 = await eval.string('{ inner = { x = 42; y = "hi"; }; z = 99; }')
        inner = v2.attr("inner")
        assert await inner.get_type() == NixType.ATTRS
        x = inner.attr("x")
        assert await x.force_as(NixType.INT) == 42
        names = await v2.attr_names()
        assert set(names) == {"inner", "z"}
        print("attr navigation: z =", await v2.attr("z").force())

        # --- force_as for type-checked scalar extraction ----------------------

        v3 = await eval.string('{ version = 4; enabled = true; name = "demo"; }')
        assert await v3.attr("version").force_as(NixType.INT) == 4
        assert await v3.attr("enabled").force_as(NixType.BOOL) is True
        assert await v3.attr("name").force_as(NixType.STRING) == "demo"
        print("force_as: all types match")

        # --- lists ------------------------------------------------------------

        v4 = await eval.string("[ 1 2 3 4 5 ]")
        assert await v4.get_type() == NixType.LIST
        assert await v4.list_length() == 5
        assert await v4.list_get(0).force() == 1
        assert await v4.list_get(-1).force() == 5
        print("list: length =", await v4.list_length())

        # --- force_json: serialize to dict/list tree (like force_deep) --------

        v5 = await eval.string('{ lib = { name = "mylib"; version = "2.0"; deps = [ "a" "b" ]; }; }')
        lib = v5.attr("lib")
        lib_json = await lib.force_json()
        assert lib_json == {"name": "mylib", "version": "2.0", "deps": ["a", "b"]}
        print("force_json:", lib_json)

    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
