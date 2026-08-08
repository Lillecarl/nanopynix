"""The environment a planner runs in, and how it emits its plan."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ._derivation import Derivation
from ._menu import Menu, check_inputs_available

# A planner runs under a bare `python3` inside a build sandbox, so this module
# imports the standard library only. `pathlib.Path` is deliberate here: a
# planner is a synchronous script, and the async-only rule of this repository
# applies to an async function.


@dataclass(frozen=True, slots=True)
class PlannerEnv:
    """What the Nix expression told the planner.

    ``system`` and ``store_dir`` come from the expression rather than from the
    interpreter, because a planner may plan for a store or a system that is
    not its own.
    """

    system: str
    store_dir: str
    out: str
    menu: Menu
    tools: dict[str, str]

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> PlannerEnv:
        """Read the planner environment that ``ddrn/nix/planner.nix`` writes.

        ``DDRN_TOOLS`` names each store path the expression passed in for the
        emitted derivation to use, as a space-separated list of
        ``name=/nix/store/...`` pairs.
        """
        env = dict(os.environ if environ is None else environ)
        tools: dict[str, str] = {}
        for item in env.get("DDRN_TOOLS", "").split():
            key, _, value = item.partition("=")
            if not value:
                raise ValueError(f"DDRN_TOOLS entry {item!r} is not name=path")
            tools[key] = value
        return cls(
            system=_required(env, "DDRN_SYSTEM"),
            store_dir=env.get("DDRN_STORE_DIR", "/nix/store"),
            out=_required(env, "out"),
            menu=Menu.from_json(env.get("DDRN_MENU", "[]")),
            tools=tools,
        )

    def tool(self, name: str) -> str:
        """The store path of a tool the expression passed in."""
        try:
            return self.tools[name]
        except KeyError:
            listed = ", ".join(sorted(self.tools)) or "none"
            raise KeyError(f"no tool named {name!r} was passed in; available: {listed}") from None

    def emit(self, derivation: Derivation, *, check: bool = True) -> None:
        """Write ``derivation`` to the output, which Nix reads back as a plan.

        ``check`` runs :func:`ddrn.check_inputs_available` first. Leave it on:
        the failure it prevents is reported by Nix long after the planner
        exits, and without a line number.
        """
        if check:
            check_inputs_available(derivation, self.menu, list(self.tools.values()), self.store_dir)
        aterm = derivation.to_aterm()
        Path(self.out).write_text(aterm, encoding="utf-8")
        print(f"ddrn: emitted {derivation.name} ({len(aterm)} bytes)", file=sys.stderr)


def _required(env: dict[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise KeyError(f"{key} is not set; a planner runs from ddrn/nix/planner.nix")
    return value
