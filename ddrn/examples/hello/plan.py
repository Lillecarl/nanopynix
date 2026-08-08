"""Emit one derivation. No menu, no dependency, no hash arithmetic."""

from __future__ import annotations

import os

import ddrn


def main() -> None:
    env = ddrn.PlannerEnv.from_environ()
    bash = env.tool("bash")
    coreutils = env.tool("coreutils")

    env.emit(
        ddrn.Derivation(
            name="hello-from-python",
            system=env.system,
            builder=f"{bash}/bin/bash",
            args=["-c", 'mkdir -p "$out" && printf "%s\\n" "$MESSAGE" > "$out/message"'],
            env={
                "PATH": f"{coreutils}/bin",
                "MESSAGE": os.environ["MESSAGE"],
            },
            # The output is floating and content-addressed, which is the
            # default of `ddrn.Output`. Nix picks the path after the build, so
            # nothing here computes a hash.
            input_srcs=[bash, coreutils],
        )
    )


if __name__ == "__main__":
    main()
