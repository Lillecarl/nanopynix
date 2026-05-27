"""Test persistence of known paths across server restarts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

from pynixd import Server
from pynixd.exceptions import OpNotImplementedError
from pynixd.operations.is_valid_path import IsValidPathRequest
from pynixd.operations.query_all_valid_paths import QueryAllValidPathsRequest
from pynixd.operations.query_valid_paths import (
    QueryValidPathsRequest,
    QueryValidPathsResponse,
)
from pynixd.store import LocalSocketStore
from pynixd.store_path import StorePath
from pynixd.types.ids import StoreId
from tests.conftest import (
    CLIENT_BIN,
    STORE_PREFIX,
    get_test_store_kwargs,
    rmtree_robust,
    run_subproc,
    server_uri,
)

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)


class NoQueryAllValidPathsStore(LocalSocketStore):
    """A store that fails QueryAllValidPaths to simulate nixbuild.net."""

    async def call(
        self,
        request,
        client=None,
        suppress_last=False,
        raise_on_error=False,
        skip_probe=False,
    ):
        if isinstance(request, QueryAllValidPathsRequest):
            raise OpNotImplementedError("QueryAllValidPaths not supported")
        return await super().call(
            request,
            client,
            suppress_last,
            raise_on_error,
            skip_probe=skip_probe,
        )


@pytest.mark.timeout(30)
async def test_known_paths_persistence(tmp_path: Path) -> None:
    """Verify that known paths for a remote store survive server restart.

    Store operations triggered:
    - QueryAllValidPaths: Queries all valid paths for persistence verification
    """
    pynixd_local_path = STORE_PREFIX / "persistence-local"
    pynixd_remote_path = STORE_PREFIX / "persistence-remote"
    rmtree_robust(pynixd_local_path)
    rmtree_robust(pynixd_remote_path)

    # 1. Start server and add a path to the remote store
    pynixd_local = LocalSocketStore(
        store_id="local",
        store_path=pynixd_local_path,
        **get_test_store_kwargs(no_probe=True),
    )
    pynixd_remote = LocalSocketStore(
        store_id="remote-stub",
        store_path=pynixd_remote_path,
        **get_test_store_kwargs(no_probe=True),
    )

    # We need a path that actually exists in the remote store so verification succeeds
    cmd = [
        str(CLIENT_BIN),
        "store",
        "add-path",
        "justfile",
        "--store",
        str(pynixd_remote_path),
    ]
    rc, stdout, stderr, stdboth = await run_subproc(cmd)
    test_path = StorePath(stdout.strip())

    async with Server(
        stores={StoreId("local"): pynixd_local, StoreId("remote"): pynixd_remote},
        ssh_port=None,
    ):
        # Manually add a path to the remote store
        # This should trigger recording to the local DB
        pynixd_remote.tracker.add_known_path(test_path)

        # Wait for flush
        assert pynixd_local.db is not None
        await pynixd_local.db.flush_regtime()

        assert test_path in pynixd_remote.tracker.known_paths

    # 2. Restart server with a store that FAILS QueryAllValidPaths
    # It should load the path from the DB and VERIFY it via QueryValidPaths.

    pynixd_local_2 = LocalSocketStore(
        store_id="local",
        store_path=pynixd_local_path,
        **get_test_store_kwargs(no_probe=True),
    )
    # Use our stub that fails QueryAllValidPaths
    pynixd_remote_2 = NoQueryAllValidPathsStore(
        store_id="remote-stub",  # Same ID is important!
        store_path=pynixd_remote_path,
        **get_test_store_kwargs(no_probe=True),
    )

    async with Server(
        stores={StoreId("local"): pynixd_local_2, StoreId("remote"): pynixd_remote_2},
        ssh_port=None,
    ):
        # Check if the path was loaded from DB and verified
        assert test_path in pynixd_remote_2.tracker.known_paths
        log.info("persistence_verified", path=test_path)


@pytest.mark.timeout(30)
async def test_known_paths_cleanup(tmp_path: Path) -> None:
    """Verify that stale cached paths are removed after verification.

    Store operations triggered:
    - QueryAllValidPaths: Queries all valid paths for cleanup verification
    """
    pynixd_local_path = STORE_PREFIX / "cleanup-local"
    pynixd_remote_path = STORE_PREFIX / "cleanup-remote"
    rmtree_robust(pynixd_local_path)
    rmtree_robust(pynixd_remote_path)

    # 1. Start server and add two paths to the remote store
    pynixd_local = LocalSocketStore(
        store_id="local",
        store_path=pynixd_local_path,
        **get_test_store_kwargs(no_probe=True),
    )
    pynixd_remote = LocalSocketStore(
        store_id="remote",
        store_path=pynixd_remote_path,
        **get_test_store_kwargs(no_probe=True),
    )

    path_valid = StorePath("/nix/store/00000000000000000000000000000001-valid")
    path_stale = StorePath("/nix/store/00000000000000000000000000000002-stale")

    async with Server(
        stores={StoreId("local"): pynixd_local, StoreId("remote"): pynixd_remote},
        ssh_port=None,
    ):
        pynixd_remote.tracker.add_known_path(path_valid)
        pynixd_remote.tracker.add_known_path(path_stale)
        assert pynixd_local.db is not None
        await pynixd_local.db.flush_regtime()

    # 2. Restart with stub that ONLY reports path_valid as valid
    # and fails QueryAllValidPaths
    class PartialVerifyStore(NoQueryAllValidPathsStore):
        async def execute(
            self,
            request,
            client=None,
            suppress_last=False,
            skip_probe=False,
        ):
            if isinstance(request, QueryValidPathsRequest):
                # Only return the valid one
                return QueryValidPathsResponse(paths={path_valid})
            return await super().execute(
                request,
                client,
                suppress_last,
                skip_probe=skip_probe,
            )

    pynixd_local_2 = LocalSocketStore(
        store_id="local",
        store_path=pynixd_local_path,
        **get_test_store_kwargs(no_probe=True),
    )
    pynixd_remote_2 = PartialVerifyStore(
        store_id="remote",
        store_path=pynixd_remote_path,
        **get_test_store_kwargs(no_probe=True),
    )

    async with Server(
        stores={StoreId("local"): pynixd_local_2, StoreId("remote"): pynixd_remote_2},
        ssh_port=None,
    ):
        # path_stale should be gone from memory
        assert path_valid in pynixd_remote_2.tracker.known_paths
        assert path_stale not in pynixd_remote_2.tracker.known_paths

        # Flush removals to DB
        assert pynixd_local_2.db is not None
        await pynixd_local_2.db.flush_regtime()

        # Check DB directly
        db_paths = await pynixd_local_2.db.get_known_paths(StoreId("remote"))
        assert path_valid in db_paths
        assert path_stale not in db_paths
        log.info("cleanup_verified")


@pytest.mark.timeout(30)
async def test_is_valid_path_isolation(tmp_path: Path) -> None:
    """Verify that IsValidPath correctly distinguishes between local and remote stores sharing a DB.

    Store operations triggered:
    - QueryAllValidPaths: Queries all valid paths for isolation verification
    """
    pynixd_local_path = STORE_PREFIX / "isolation-local"
    pynixd_remote_path = STORE_PREFIX / "isolation-remote"
    rmtree_robust(pynixd_local_path)
    rmtree_robust(pynixd_remote_path)

    pynixd_local = LocalSocketStore(
        store_id="local",
        store_path=pynixd_local_path,
        **get_test_store_kwargs(no_probe=True),
    )
    pynixd_remote = LocalSocketStore(
        store_id="remote",
        store_path=pynixd_remote_path,
        **get_test_store_kwargs(no_probe=True),
    )

    async with Server(
        stores={StoreId("local"): pynixd_local, StoreId("remote"): pynixd_remote},
        ssh_port=None,
    ):
        # 1. Add a path to LOCAL store only
        cmd = [
            str(CLIENT_BIN),
            "store",
            "add-path",
            "justfile",
            "--store",
            str(pynixd_local_path),
        ]
        rc, stdout, stderr, _ = await run_subproc(cmd)
        assert rc == 0
        local_path = StorePath(stdout.strip())

        # Local store should know it (via ValidPaths fast-path)

        resp_local = await IsValidPathRequest(path=local_path).execute(pynixd_local)
        assert resp_local.valid

        # Remote store should NOT know it, even if it shares pynixd_local.db
        # because pynixd_local.db.store_path (local) != pynixd_remote.store_path (remote)
        resp_remote = await IsValidPathRequest(path=local_path).execute(pynixd_remote)
        assert not resp_remote.valid
