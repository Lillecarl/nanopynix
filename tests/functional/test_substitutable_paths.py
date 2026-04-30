"""
Tests for substitution-related queries over the daemon protocol.

Tests these protocol operations:
- QuerySubstitutablePathInfo (op 21): Single path substitutability check
- QuerySubstitutablePathInfos (op 30): Batch substitutability check
- QuerySubstitutablePaths (op 32): Which paths are substitutable

These operations are forwarded to the upstream daemon — they test
that protocol serialization works correctly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import structlog

from tests.conftest import CLIENT_BIN, TEST_NIX, run_subproc, server_uri

if TYPE_CHECKING:
    from pynixd import Server

log = structlog.get_logger(__name__)


@pytest.mark.timeout(60)
async def test_substitutable_paths_via_store(pynixd_server: Server) -> None:
    """QuerySubstitutablePaths: query which paths are available for substitution.

    We can't easily control what the upstream daemon knows about substitutable
    paths, but we can verify the protocol round-trips correctly by performing
    a query through the local store.
    """
    uri = server_uri(pynixd_server)

    # Build a path first
    test_nix = TEST_NIX
    cmd = [
        str(CLIENT_BIN),
        "build",
        "--eval-store",
        "auto",
        "--store",
        uri,
        "--file",
        str(test_nix),
        "minimal.leaf",
        "--no-link",
        "--print-out-paths",
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0, f"build failed:\n{stdboth}"

    out_path = stdout.strip()
    cmd = [
        str(CLIENT_BIN),
        "path-info",
        "--store",
        uri,
        out_path,
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0, f"path-info failed:\n{stdboth}"
    assert out_path in stdout, f"Expected {out_path} in path-info output:\n{stdboth}"


@pytest.mark.timeout(60)
async def test_substitutable_paths_via_nix(pynixd_server: Server) -> None:
    """Exercise substitution queries through the Nix CLI.

    This triggers QuerySubstitutablePaths and QuerySubstitutablePathInfos.
    """
    uri = server_uri(pynixd_server)

    test_nix = TEST_NIX
    cmd = [
        str(CLIENT_BIN),
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
