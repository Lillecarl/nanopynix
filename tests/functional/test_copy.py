"""Tests for copying store paths between stores."""

from __future__ import annotations

from pynixd import Server
from tests.conftest import (
    NIX_BIN,
    run_subproc,
)


async def test_copy(pynixd_server: Server):
    """Copy paths between two stores via UDS.

    Store operations triggered:
    - AddMultipleToStore: Adds multiple paths to store
    - QueryValidPaths: Queries valid paths
    - RegisterDrvOutput: Registers derivation output
    """

    await run_subproc([NIX_BIN, "build", "nixpkgs#hello"])
    await run_subproc(
        [
            NIX_BIN,
            "copy",
            "--from",
            "daemon",
            "--to",
            pynixd_server.uri(),
            "nixpkgs#hello",
        ],
    )
