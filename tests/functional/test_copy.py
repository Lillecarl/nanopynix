"""Tests for copying store paths between stores."""

from __future__ import annotations


from pynixd import Server
from pynixd.store import LocalSocketStore
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    get_test_store_kwargs,
    run_subproc,
    rmtree_robust,
)

LOCAL_STORE = STORE_PREFIX / "local"


async def test_copy():
    """Copy paths between two stores via UDS."""

    rmtree_robust(LOCAL_STORE)
    src_store = LocalSocketStore(
        id="local",
        store_path=LOCAL_STORE,
        **get_test_store_kwargs(),
    )

    async with Server(
        local_store=src_store,
        ssh_port=0,
    ) as server:
        await run_subproc([NIX_BIN, "build", "nixpkgs#hello"])
        await run_subproc(
            [NIX_BIN, "copy", "--from", "daemon", "--to", server.uri(), "nixpkgs#hello"]
        )
