"""Test Role-Based Access Control (RBAC)."""

from __future__ import annotations

import structlog

from pynixd import Server
from tests.conftest import (
    NIX_BIN,
    SESSION_STORE_PREFIX,
    run_subproc,
)

log = structlog.get_logger(__name__)


async def test_rbac_ssh_admin_vs_user(pynixd_server: Server) -> None:
    """Verify that SSH admin can GC, but SSH user cannot.

    Store operations triggered:
    - None: This test only checks RBAC authentication/authorization without triggering Store operations
    """
    uri_user = f"ssh-ng://regular-user@127.0.0.1:{pynixd_server.port}"
    cmd_user = [str(NIX_BIN), "store", "gc", "--store", uri_user]
    rc_user, stdout_user, stderr_user, stdboth_user = await run_subproc(
        cmd_user, expected_retcode=None
    )
    assert rc_user != 0
    assert "requires administrative privileges" in stdboth_user

    uri_admin = f"ssh-ng://admin-user@127.0.0.1:{pynixd_server.port}"
    cmd_admin = [str(NIX_BIN), "store", "gc", "--store", uri_admin]
    rc_admin, stdout_admin, stderr_admin, stdboth_admin = await run_subproc(cmd_admin)
    assert rc_admin == 0


async def test_rbac_unix_implicit_admin(pynixd_server: Server) -> None:
    """Verify that Unix socket connections are implicit admins.

    Store operations triggered:
    - None: This test only checks RBAC authentication/authorization without triggering Store operations
    """
    socket_path = SESSION_STORE_PREFIX / "pynixd.sock"
    local_path = pynixd_server.local_store.store_path
    uri = f"unix://{socket_path}?root={local_path}"
    cmd = [str(NIX_BIN), "store", "gc", "--store", uri]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    assert rc == 0
