"""DAG build tests."""

from __future__ import annotations

import logging
from pathlib import Path

import structlog

from pynixd import Server
from pynixd.instance import NixImplementation
from pynixd.store import LocalSocketStore
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    get_test_store_kwargs,
    run_subproc,
    set_log_levels,
)

log = structlog.get_logger(__name__)


async def test_dag_builders() -> None:
    """Build test.nix .dag via --builders."""
    test_nix = Path("test.nix")
    local_store = LocalSocketStore(
        id="local",
        store_path=STORE_PREFIX / "dag-builders-local",
        **get_test_store_kwargs(),
    )
    builder_store = LocalSocketStore(
        id="builder",
        store_path=STORE_PREFIX / "dag-builders-builder",
        **get_test_store_kwargs(),
    )

    async with Server(
        local_store=local_store, stores={"builder": builder_store}, ssh_port=0
    ) as server:
        uri = server.builder_uri(implementation=NixImplementation.NIX, max_jobs=4)
        cmd = [
            str(NIX_BIN),
            "build",
            "--builders",
            uri,
            "--file",
            str(test_nix),
            "dag",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr, _ = await run_subproc(cmd)
        assert rc == 0, f"""build failed:
{stderr}"""


async def test_dag_store() -> None:
    """Build test.nix .dag via --store."""
    test_nix = Path("test.nix")
    local_store = LocalSocketStore(
        id="local",
        store_path=STORE_PREFIX / "dag-store-local",
        **get_test_store_kwargs(),
    )
    builder_store = LocalSocketStore(
        id="builder",
        store_path=STORE_PREFIX / "dag-store-builder",
        **get_test_store_kwargs(),
    )

    # AddToStore is muted to INFO because it produces ~4500 DEBUG log lines
    # (one per path being added). This is 100% confirmed working — the missing
    # DEBUG output is NOT the cause of any test failure. Do NOT remove this
    # silencing unless you want to drown the AI in thousands of log lines.
    with set_log_levels({"pynixd.op.AddToStore": logging.INFO}):
        async with Server(
            local_store=local_store, stores={"builder": builder_store}, ssh_port=0
        ) as server:
            uri = server.uri(implementation=NixImplementation.NIX)
            cmd = [
                str(NIX_BIN),
                "build",
                "--store",
                uri,
                "--file",
                str(test_nix),
                "dag",
                "--no-link",
                "--print-out-paths",
            ]
            rc, stdout, stderr, _ = await run_subproc(cmd)
            assert rc == 0, f"""build failed:
{stderr}"""
