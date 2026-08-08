"""Write Nix derivations from Python, for the ``dynamic-derivations`` feature.

A *planner* is an ordinary derivation whose output is text-hashed. Nix reads
that output back as a derivation, and ``builtins.outputOf`` names the output of
*that* derivation. The planner is therefore a program that decides, at build
time, what Nix builds next.

This package is what a planner is written against. It has no dependency
outside the standard library, because a planner runs under a bare ``python3``
inside a build sandbox.

.. code-block:: python

    env = ddrn.PlannerEnv.from_environ()
    wanted = [c for c in env.menu if applies(c.meta)]
    env.emit(
        ddrn.Derivation(
            name="venv",
            system=env.system,
            builder=f"{env.tool('bash')}/bin/bash",
            args=["-c", script],
            env={"WHEELS": " ".join(c.output() for c in wanted)},
            input_srcs=[env.tool("bash"), env.tool("coreutils")],
            input_drvs=env.menu.select(wanted),
        )
    )

Read ``ddrn/README.md`` for what the feature can and cannot express, and for
the measurements behind each statement.
"""

from __future__ import annotations

from ._derivation import (
    PLACEHOLDER_OUT as PLACEHOLDER_OUT,
    Derivation as Derivation,
    FixedMethod as FixedMethod,
    IngestionMethod as IngestionMethod,
    Output as Output,
)
from ._menu import (
    Candidate as Candidate,
    Menu as Menu,
    check_inputs_available as check_inputs_available,
)
from ._planner import PlannerEnv as PlannerEnv
from ._storepath import (
    make_fixed_output_path as make_fixed_output_path,
    make_store_path as make_store_path,
    nix_base32 as nix_base32,
    sri_to_hex as sri_to_hex,
)

__all__ = [
    "PLACEHOLDER_OUT",
    "Candidate",
    "Derivation",
    "FixedMethod",
    "IngestionMethod",
    "Menu",
    "Output",
    "PlannerEnv",
    "check_inputs_available",
    "make_fixed_output_path",
    "make_store_path",
    "nix_base32",
    "sri_to_hex",
]
