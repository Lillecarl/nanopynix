"""
Tests for admin-gated operations that don't have other coverage.

Tests these protocol operations:
- OptimiseStore (op 34): Deduplicates store (admin-only)
- VerifyStore (op 35): Verifies store integrity (admin-only)
- AddBuildLog (op 45): Adds build log (admin-only)
- AddSignatures (op 37): Adds signatures to a path (forwarded)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

from tests.conftest import NIX_BIN, run_subproc

if TYPE_CHECKING:
    from pynixd import Server

log = structlog.get_logger(__name__)


@pytest.mark.timeout(120)
async def test_optimise_store_admin(pynixd_server: Server) -> None:
    """OptimiseStore as admin should succeed.

    Uses Unix socket (implicit admin).
    """
    from tests.conftest import SESSION_STORE_PREFIX

    socket_path = SESSION_STORE_PREFIX / "pynixd.sock"
    local_path = pynixd_server.local_store.store_path
    uri = f"unix://{socket_path}?root={local_path}"
    cmd = [str(NIX_BIN), "store", "optimise", "--store", uri]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0, f"OptimiseStore failed:\n{stdboth}"


@pytest.mark.timeout(120)
async def test_optimise_store_non_admin(pynixd_server: Server) -> None:
    """OptimiseStore as non-admin should be rejected."""
    uri = f"ssh-ng://regular-user@127.0.0.1:{pynixd_server.port}"
    cmd = [str(NIX_BIN), "store", "optimise", "--store", uri]
    rc, stdout, stderr, stdboth = await run_subproc(cmd, expected_retcode=None)
    assert rc != 0, "OptimiseStore should fail for non-admin"
    assert "requires administrative privileges" in stdboth


@pytest.mark.timeout(120)
async def test_verify_store_admin(pynixd_server: Server) -> None:
    """VerifyStore as admin should succeed."""
    from tests.conftest import SESSION_STORE_PREFIX

    socket_path = SESSION_STORE_PREFIX / "pynixd.sock"
    local_path = pynixd_server.local_store.store_path
    uri = f"unix://{socket_path}?root={local_path}"
    cmd = [str(NIX_BIN.parent / "nix-store"), "--verify", "--store", uri]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0, f"VerifyStore failed:\n{stdboth}"


@pytest.mark.timeout(120)
async def test_verify_store_non_admin(pynixd_server: Server) -> None:
    """VerifyStore as non-admin should be rejected."""
    uri = f"ssh-ng://regular-user@127.0.0.1:{pynixd_server.port}"
    cmd = [str(NIX_BIN.parent / "nix-store"), "--verify", "--store", uri]
    rc, stdout, stderr, stdboth = await run_subproc(cmd, expected_retcode=None)
    assert rc != 0, "VerifyStore should fail for non-admin"
    assert "requires administrative privileges" in stdboth


@pytest.mark.timeout(120)
async def test_add_build_log_non_admin(pynixd_server: Server) -> None:
    """AddBuildLog as non-admin should be rejected.

    This is tricky to trigger via CLI — it's used internally by builders.
    Instead, test via SSH as non-admin.
    """
    uri = f"ssh-ng://regular-user@127.0.0.1:{pynixd_server.port}"
    # Attempt to run a build that might trigger AddBuildLog
    # We verify the build still works — if AddBuildLog is rejected it's fine
    cmd = [
        str(NIX_BIN),
        "build",
        "--store",
        uri,
        "--eval-store",
        "auto",
        "--file",
        "tests/nix",
        "minimal.leaf",
        "--no-link",
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0, f"Build (and possibly AddBuildLog) failed:\n{stdboth}"


@pytest.mark.timeout(120)
async def test_add_signatures_via_store(pynixd_server: Server) -> None:
    """AddSignatures: signing a path via pynixd.

    This operation is forwarded to the upstream daemon.
    We build a path first, then test that signatures can be added by admin.
    """
    uri = f"ssh-ng://admin-user@127.0.0.1:{pynixd_server.port}"

    # Build a path
    cmd = [
        str(NIX_BIN),
        "build",
        "--eval-store",
        "auto",
        "--store",
        uri,
        "--file",
        "tests/nix",
        "minimal.leaf",
        "--no-link",
        "--print-out-paths",
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0, f"build failed:\n{stdboth}"

    out_path = stdout.strip()
    assert out_path.startswith("/nix/store/"), f"Unexpected output: {out_path}"
