"""Functional test for pynixd extension delegation between servers."""

from __future__ import annotations

from pathlib import Path

import asyncssh
import pytest
import structlog

from pynixd import Server
from pynixd.config import LocalSocketStoreSpec, SSHSubprocessStoreSpec
from pynixd.operations.query_all_valid_paths import QueryAllValidPathsRequest
from pynixd.operations.query_path_infos import (
    QueryPathInfosRequest,
)
from pynixd.store import LocalSocketStore, SSHSubprocessStore
from pynixd.store_path import StorePath
from pynixd.testing import get_test_value
from pynixd.types.ids import StoreId
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    make_test_spec,
    rmtree_robust,
    run_subproc,
)
from tests.test_features import TestFeatures as F

log = structlog.get_logger(__name__)


async def _pick_random_path(store: LocalSocketStore) -> str:
    """Pick an arbitrary valid path from the store."""
    resp = await store.execute(QueryAllValidPathsRequest())
    all_paths = resp.paths
    assert all_paths, "Store has no paths?!"
    return str(next(iter(all_paths)))


@pytest.mark.covers(F.EXTENSION_DELEGATION | F.STORE_LOCAL)
@pytest.mark.timeout(60)
async def test_extension_delegation(tmp_path: Path) -> None:
    """Test that pynixd can delegate extension OPs to other pynixd instances.

    Store operations triggered:
    - QueryAllValidPaths: Queries all valid paths
    - QueryPathInfo: Queries path info
    - QueryPathInfos: Queries path infos
    """

    # 0. Setup SSH keys and stores
    key = asyncssh.generate_private_key("ssh-rsa")

    store_b_path = STORE_PREFIX / "extension-delegation-store-b"
    rmtree_robust(store_b_path)
    store_b_path.mkdir(parents=True, exist_ok=True)
    store_b = LocalSocketStore(
        make_test_spec(
            store_id="b-local",
            store_path=store_b_path,
            no_probe=True,
        ),
    )
    await store_b.ensure_daemon()

    # Add a path to store B
    cmd = [
        str(NIX_BIN),
        "store",
        "add-path",
        "--store",
        str(store_b_path),
        "README.md",
    ]
    rc, stdout, stderr, _ = await run_subproc(cmd)
    assert rc == 0
    path = StorePath(stdout.strip())

    # 1. Start Server B (Builder)
    async with Server(
        stores={StoreId("local"): store_b},
        ssh_port=0,
    ) as server_b:
        port_b = server_b.port
        log.info("server_b_started", port=port_b)

        # 2. Start Server A (Proxy)
        # It has server_b as a store.
        store_a_b = SSHSubprocessStore(
            SSHSubprocessStoreSpec(
                store_id=StoreId("builder-b"),
                host="127.0.0.1",
                port=port_b,
                username=server_b.username,
                client_keys=[key],
                nix_bin=str(NIX_BIN),
                monitor=False,
            ),
        )

        # Server A's local store doesn't have a DB and doesn't support extensions
        store_a = LocalSocketStore(
            make_test_spec(
                store_id="a-local",
                store_path=Path("/"),
                no_probe=True,
                use_db=False,
            ),
        )

        unix_path_a = tmp_path / "server-a.sock"

        async with Server(
            stores={StoreId("local"): store_a, StoreId("builder-b"): store_a_b},
            ssh_port=0,
            unix_path=unix_path_a,
        ) as server_a:
            log.info("server_a_started", port=server_a.port, unix=unix_path_a)

            # Trigger feature detection
            await store_a_b.execute(QueryAllValidPathsRequest())

            log.info("server_b_features", features=store_a_b.features)
            assert "QueryPathInfos" in store_a_b.features

            # Now try to execute QueryPathInfos on store_a_b
            req = QueryPathInfosRequest(paths={path})
            resp = await store_a_b.execute(req)

            assert path in resp.infos
            log.info("query_path_infos_success", path=path)

            # 3. Test delegation: Connect to Server A via Unix socket
            # This will trigger DaemonProxy.execute which should delegate to Server B
            # because Server A's local store (store_a) doesn't have the info.
            client_store = LocalSocketStore(
                LocalSocketStoreSpec(
                    store_id=StoreId("client"),
                    socket_path=unix_path_a,
                ),
            )
            resp_a = await client_store.execute(req)

            assert path in resp_a.infos
            log.info("delegated_query_path_infos_success", path=path)

            # 4. Verify fast path was taken via internal instrumentation
            assert get_test_value("QueryPathInfos_delegated") is True
            log.info("delegation_path_verified_via_instrumentation")
