"""Evaluate Nix expressions and navigate results.

Run with::

    python docs/examples/eval_example.py
"""

# ruff: noqa: T201

from __future__ import annotations

import asyncio

from nanopynix import NixType
from nanopynix.rpc import Session


async def main() -> None:
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        # --- eval.string: inline Nix expressions -------------------------------

        v = await eval.string('{ a = 1; b = "hello"; c = true; d = [ 10 20 ]; }')
        assert await v.get_type() == NixType.ATTRS

        # to_python converts the entire value tree to nested Python dicts/lists.
        result = await v.to_python()
        assert result == {"a": 1, "b": "hello", "c": True, "d": [10, 20]}
        print("to_python:", result)

        # --- attr navigation without full deep conversion ---------------------

        v2 = await eval.string('{ inner = { x = 42; y = "hi"; }; z = 99; }')
        inner = v2.attr("inner")
        assert await inner.get_type() == NixType.ATTRS
        x = inner.attr("x")
        assert await x.as_int() == 42
        names = await v2.attr_names()
        assert set(names) == {"inner", "z"}
        print("attr navigation: z =", await v2.attr("z").to_python())

        # --- strict accessors for type-checked scalar extraction ---------------

        v3 = await eval.string('{ version = 4; enabled = true; name = "demo"; }')
        assert await v3.attr("version").as_int() == 4
        assert await v3.attr("enabled").as_bool() is True
        assert await v3.attr("name").as_string() == "demo"
        print("strict accessors: all types match")

        # --- lists ------------------------------------------------------------

        v4 = await eval.string("[ 1 2 3 4 5 ]")
        assert await v4.get_type() == NixType.LIST
        assert await v4.list_length() == 5
        assert await v4.list_get(0).as_int() == 1
        assert await v4.list_get(-1).as_int() == 5
        print("list: length =", await v4.list_length())

        # --- to_python: serialize to dict/list tree (like to_python) --------

        v5 = await eval.string('{ lib = { name = "mylib"; version = "2.0"; deps = [ "a" "b" ]; }; }')
        lib = v5.attr("lib")
        lib_json = await lib.to_python()
        assert lib_json == {"name": "mylib", "version": "2.0", "deps": ["a", "b"]}
        print("to_python:", lib_json)

    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
