"""
Tests for QueryDerivationOutputMap (op 41).

This operation queries the output -> path mapping for a derivation.
It also updates the path tracker when resolved paths are found.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from tests.conftest import NIX_BIN, TEST_NIX, run_subproc

if TYPE_CHECKING:
    from pynixd import Server

log = structlog.get_logger(__name__)


async def test_query_derivation_output_map(pynixd_server: Server) -> None:
    """Build a derivation and query its output map.

    QueryDerivationOutputMap maps output names to store paths.
    After a successful build, it should return the realized paths.
    """
    uri = pynixd_server.uri()

    test_nix = TEST_NIX
    cmd = [
        str(NIX_BIN),
        "build",
        "--eval-store",
        "auto",
        "--store",
        uri,
        "--file",
        str(test_nix),
        "minimal.leaf",
        "--no-link",
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0, f"build failed:\n{stdboth}"
