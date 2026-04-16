"""Test Role-Based Access Control (RBAC)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
import structlog
from pynixd import Server
from pynixd.store import LocalSocketStore
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    get_test_store_kwargs,
    run_subproc,
    rmtree_robust,
)

log = structlog.get_logger(__name__)


@pytest.mark.asyncio
async def test_rbac_ssh_admin_vs_user(tmp_path: Path) -> None:
    """Verify that SSH admin can GC, but SSH user cannot."""
    async with asyncio.timeout(30):
        pynixd_local_path = STORE_PREFIX / "rbac-ssh-local"
        rmtree_robust(pynixd_local_path)

        pynixd_local = LocalSocketStore(
            id="local",
            store_path=pynixd_local_path,
            **get_test_store_kwargs(),
        )

        # 1. Start server with 'admin-user' as admin
        async with Server(
            local_store=pynixd_local,
            ssh_port=0,
            admin_users={"admin-user"},
        ) as server:
            # 2. Try GC as 'regular-user' -> should fail
            uri_user = f"ssh-ng://regular-user@127.0.0.1:{server.port}"
            cmd_user = [str(NIX_BIN), "store", "gc", "--store", uri_user]
            # Use empty env to avoid picking up system SSH keys that might confuse things,
            # though pynixd accepts everything.
            rc_user, stdout_user, stderr_user, stdboth_user = await run_subproc(
                cmd_user, expected_retcode=None
            )
            assert rc_user != 0
            assert "requires administrative privileges" in stdboth_user

            # 3. Try GC as 'admin-user' -> should succeed
            uri_admin = f"ssh-ng://admin-user@127.0.0.1:{server.port}"
            cmd_admin = [str(NIX_BIN), "store", "gc", "--store", uri_admin]
            rc_admin, stdout_admin, stderr_admin, stdboth_admin = await run_subproc(
                cmd_admin
            )
            assert rc_admin == 0


@pytest.mark.asyncio
async def test_rbac_unix_implicit_admin(tmp_path: Path) -> None:
    """Verify that Unix socket connections are implicit admins."""
    async with asyncio.timeout(30):
        pynixd_local_path = STORE_PREFIX / "rbac-unix-local"
        rmtree_robust(pynixd_local_path)
        socket_path = pynixd_local_path / "socket"

        pynixd_local = LocalSocketStore(
            id="local",
            store_path=pynixd_local_path,
            **get_test_store_kwargs(),
        )

        async with Server(
            local_store=pynixd_local,
            unix_path=socket_path,
        ):
            # Try GC over Unix socket -> should succeed
            # root= is needed so nix knows where the store is physically
            uri = f"unix://{socket_path}?root={pynixd_local_path}"
            cmd = [str(NIX_BIN), "store", "gc", "--store", uri]
            rc, stdout, stderr, stdboth = await run_subproc(cmd)
            assert rc == 0
