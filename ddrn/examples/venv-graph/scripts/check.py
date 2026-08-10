"""Report what the environment holds, from inside the environment.

`check.nix` runs this with the `bin/python` of the environment that the graph
built. Nothing here adds a path: an import that works proves that the
environment finds its own `site-packages`.
"""

from __future__ import annotations

import sys
from importlib import metadata

import certifi
import charset_normalizer
import idna


def main() -> int:
    print(f"interpreter  {sys.executable}")
    print(f"prefix       {sys.prefix}")
    print(f"idna         {idna.__version__} {idna.encode('ドメイン.テスト').decode()}")
    print(f"certifi      {certifi.where().rsplit('/', 1)[-1]}")
    print(f"charset      {charset_normalizer.__version__}")

    # **This is what `unzip` cannot give.** A distribution is discoverable only
    # when an installer wrote its `.dist-info`, with `RECORD` and `METADATA`.
    found = sorted((dist.name or "?", dist.version) for dist in metadata.distributions())
    print("distributions")
    for name, version in found:
        print(f"  {name}=={version}")

    # `idna` came from a source distribution, so its entry point exists only
    # because the installer read `entry_points.txt` and wrote the script.
    entry_points = metadata.distribution("idna").entry_points
    print(f"entry points {sorted(f'{ep.name}={ep.value}' for ep in entry_points)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
