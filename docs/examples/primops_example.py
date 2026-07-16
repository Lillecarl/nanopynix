"""Register custom Python primops and use the built-in YAML primops.

Run with::

    python docs/examples/primops_example.py
"""

# ruff: noqa: T201

from __future__ import annotations

import asyncio

from nanopynix import NixType, PrimOpSpec, Session, yaml_primops


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
        assert await doubled.force_as(NixType.INT) == 42
        print("pyDouble 21 =", await doubled.force())

        # --- built-in YAML primops ----------------------------------------

        parsed = await eval_.string('builtins.fromYAML "a: 1\\nb: [2, 3]\\n"')
        assert await parsed.force_json() == {"a": 1, "b": [2, 3]}
        print("fromYAML:", await parsed.force_json())

        rendered = await eval_.string("builtins.toYAML { a = 1; b = [ 2 3 ]; }")
        rendered_str = await rendered.force_as(NixType.STRING)
        assert rendered_str == "a: 1\nb:\n- 2\n- 3\n"
        print("toYAML:\n" + rendered_str)

    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
