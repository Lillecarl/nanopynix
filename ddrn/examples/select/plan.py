"""Pick a subset of the menu, and depend on that subset only.

The rejected candidates keep their `.drv` file in the store, because Nix wrote
every `.drv` at instantiation. Their *outputs* are never built.
"""

from __future__ import annotations

import os
import sys

import ddrn


def main() -> None:
    env = ddrn.PlannerEnv.from_environ()
    wanted_tags = os.environ["WANTED_TAGS"].split()

    chosen = [candidate for candidate in env.menu if candidate.meta.get("tag") in wanted_tags]
    rejected = [candidate for candidate in env.menu if candidate not in chosen]
    print(f"plan: {len(chosen)} chosen, {len(rejected)} rejected", file=sys.stderr)
    for candidate in rejected:
        print(f"plan: rejected {candidate.name} (tag {candidate.meta.get('tag')})", file=sys.stderr)

    bash = env.tool("bash")
    coreutils = env.tool("coreutils")
    script = 'mkdir -p "$out" && for path in $CHOSEN; do cat "$path" >> "$out/manifest"; done'

    env.emit(
        ddrn.Derivation(
            name="selected-wheels",
            system=env.system,
            builder=f"{bash}/bin/bash",
            args=["-c", script],
            env={
                "PATH": f"{coreutils}/bin",
                "CHOSEN": " ".join(candidate.output() for candidate in chosen),
            },
            input_srcs=[bash, coreutils],
            # This is the line that makes Nix build the chosen candidates, and
            # only the chosen ones.
            input_drvs=env.menu.select(chosen),
        )
    )


if __name__ == "__main__":
    main()
