"""Tests for copying store paths between stores."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conftest import (
    NIX_BIN,
    run_subproc,
)

if TYPE_CHECKING:
    from pynixd import Server


@pytest.mark.timeout(60)
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
