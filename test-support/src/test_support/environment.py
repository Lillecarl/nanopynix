"""What the run is inside, when that decides whether a test can run at all.

A Nix build sandbox has no network and no loopback interface, so a test that
binds a socket cannot pass there and is not broken. Only the suite knows which
of its tests those are; this module answers the other half of the question,
which is where the run is.

**Issue #130 named this function as the example of the split it asked for.**
``grpclib-transports`` wrote its own copy in its own conftest, because the
shared helpers were under ``tests/support/`` and imported as
``tests.support.<name>`` -- a name that resolves from the repository rootdir
and from no subproject's rootdir. The copy was not a mistake by its author.
It was the only thing that suite could do.

The name says Nix and this project does not load Nix, which is the line the
package docstring draws. The two variables below come from the environment,
so the answer costs no import and no store.
"""

from __future__ import annotations

import os


def in_nix_build_sandbox() -> bool:
    """Whether this process runs inside a Nix build, or a pure Nix shell.

    ``NIX_BUILD_TOP`` is the build directory, and Nix sets it for every
    derivation. ``IN_NIX_SHELL`` is ``pure`` only when the caller asked for a
    shell with the same isolation, which is the case the second half covers.
    """
    return "NIX_BUILD_TOP" in os.environ or os.environ.get("IN_NIX_SHELL") == "pure"
