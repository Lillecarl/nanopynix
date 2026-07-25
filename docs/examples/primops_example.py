"""Register custom Python primops and use the built-in YAML primops.

Requires Nix >= 2.32 — primop registration is broken on Nix 2.31 and isn't
expected to be fixed there.

Run with::

    python docs/examples/primops_example.py
"""

# ruff: noqa: T201

from __future__ import annotations

import asyncio

from nanopynix import PrimOpSpec, yaml_primops
from nanopynix.rpc import Session


async def main() -> None:
    # --- a primop backed by a Python callable in *this* process --------
    #
    # rpc=True routes each call from the worker back to the client process
    # over the worker's RPC backchannel — the callable never has to be
    # importable by the worker (unlike ``PrimOpSpec.import_path``).

    double_spec = PrimOpSpec(
        name="pyDouble",
        arity=1,
        args=["x"],
        doc="Double an integer using a Python callable.",
        rpc=True,
    )

    def _py_double(x: int) -> int:
        return x * 2

    async with (
        Session(
            primops=[double_spec, *yaml_primops()],
            primop_callables={"pyDouble": _py_double},
        ) as session,
        session.store() as store,
        session.eval(store) as eval_,
    ):
        # --- custom RPC-backed primop ------------------------------------

        doubled = await eval_.string("builtins.pyDouble 21")
        assert await doubled.as_int() == 42
        print("pyDouble 21 =", await doubled.as_int())

        # --- built-in YAML primops ----------------------------------------

        parsed = await eval_.string('builtins.fromYAML "a: 1\\nb: [2, 3]\\n"')
        assert await parsed.to_python() == {"a": 1, "b": [2, 3]}
        print("fromYAML:", await parsed.to_python())

        rendered = await eval_.string("builtins.toYAML { a = 1; b = [ 2 3 ]; }")
        rendered_str = await rendered.as_string()
        assert rendered_str == "a: 1\nb:\n- 2\n- 3\n"
        print("toYAML:\n" + rendered_str)

    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
