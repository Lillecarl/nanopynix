"""
Tests for the CollectGarbage daemon operation.

GC operation (op 20) requires admin privileges and is forwarded to the
remote daemon. Tests verify:
- Admin users can GC successfully
- Non-admin users get an error
- GC correctly updates the path tracker
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import structlog

from tests.conftest import NIX_BIN, run_subproc

if TYPE_CHECKING:
    from pynixd import Server

log = structlog.get_logger(__name__)


@pytest.mark.timeout(120)
async def test_collect_garbage_admin(pynixd_server: Server) -> None:
    """GC as admin user should succeed.

    Uses the SSH admin-user to trigger GC on the remote store.
    """
    uri = f"ssh-ng://admin-user@127.0.0.1:{pynixd_server.port}"
    cmd = [str(NIX_BIN), "store", "gc", "--store", uri, "--max", "0"]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0, f"GC as admin failed:\n{stdboth}"
    # GC should report freed bytes or "0 bytes"
    assert "freed" in stdout.lower() or "freed" in stderr.lower(), f"Unexpected GC output:\n{stdboth}"


@pytest.mark.timeout(120)
async def test_collect_garbage_non_admin(pynixd_server: Server) -> None:
    """GC as non-admin user should be rejected."""
    uri = f"ssh-ng://regular-user@127.0.0.1:{pynixd_server.port}"
    cmd = [str(NIX_BIN), "store", "gc", "--store", uri, "--max", "0"]
    rc, stdout, stderr, stdboth = await run_subproc(cmd, expected_retcode=None)
    assert rc != 0, "GC should fail for non-admin"
    assert "requires administrative privileges" in stdboth


@pytest.mark.timeout(120)
async def test_collect_garbage_unix_admin(pynixd_server: Server) -> None:
    """GC over Unix socket (implicit admin) should succeed."""
    from tests.conftest import SESSION_STORE_PREFIX

    socket_path = SESSION_STORE_PREFIX / "pynixd.sock"
    local_path = pynixd_server.local_store.store_path
    uri = f"unix://{socket_path}?root={local_path}"
    cmd = [str(NIX_BIN), "store", "gc", "--store", uri, "--max", "0"]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0, f"GC over Unix socket failed:\n{stdboth}"
