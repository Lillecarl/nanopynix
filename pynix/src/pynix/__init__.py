from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


def main() -> None:
    # Strip cwd / "" from sys.path BEFORE importing nanopynix so this harness
    # proves the package resolves from the installed environment, not the
    # project source tree lying around on disk. The lazy import below is
    # intentional and required for the test to mean anything.
    cwd = str(Path.cwd())
    sys.path[:] = [p for p in sys.path if p not in ("", ".", cwd)]

    import nanopynix

    expr = """
    let pkgs = import /etc/nixpkgs {}; in
    {
      name = "pynix";
      version = "0.1.0";
      count = 42;
      ratio = 3.14;
      nested = {
        greeting = "hello";
        value = 100;
        pi = 3.14159;
      };
      tags = [ "a" "b" "c" ];
      nums = [ 1 2 3 ];
      textFile = pkgs.writeText "textFile" "content";
    }
    """

    async def _run() -> None:
        async with nanopynix.Session() as nix, nix.eval() as session:
            root = await session.string(expr)
            value = await root.force_json()
            rendered = json.dumps(value, sort_keys=True, indent=2)
            print("pynix: evaluated and serialized:")
            print(rendered)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
