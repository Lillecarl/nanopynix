"""Report what the environment holds, from inside the environment.

`check.nix` runs this with the `bin/python` of the environment that the graph
built. Nothing here adds a path: an import that works proves that the
environment finds its own `site-packages`.
"""

from __future__ import annotations

import os
import sys
from importlib import metadata
from typing import cast

import certifi
import charset_normalizer
import idna
import myapp
import ninja  # pyright: ignore[reportMissingImports] -- only the graph installs this, from a wheel of the lock file


def main() -> int:
    print(f"interpreter  {sys.executable}")
    print(f"prefix       {sys.prefix}")
    print(f"idna         {idna.__version__} {idna.encode('ドメイン.テスト').decode()}")
    print(f"certifi      {certifi.where().rsplit('/', 1)[-1]}")
    print(f"charset      {charset_normalizer.__version__}")
    ninja_version = cast("str", ninja.__version__)  # pyright: ignore[reportUnknownMemberType] -- the import above is unresolved, so this attribute has no type
    print(f"ninja        {ninja_version}")

    # **The editable reads a tree that is not in this environment.** The `.pth`
    # of `myapp` expands `$DDRN_EDITABLE_ROOT` when the interpreter starts, so
    # this line reports whichever tree the check derivation named.
    print(f"editable     {os.environ['DDRN_EDITABLE_ROOT']}")
    print(f"  {myapp.greet()}")
    print(f"  loaded from {myapp.__file__}")

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
