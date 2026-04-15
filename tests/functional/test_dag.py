"""DAG build tests."""

from __future__ import annotations

import logging
import os
import asyncio
from pathlib import Path

import structlog

from pynixd import Server
from pynixd.store import LocalSocketStore, get_current_system
from tests.conftest import (
    NIX_BIN,
    STORE_PREFIX,
    get_test_store_kwargs,
    run_subproc,
    set_log_levels,
    rmtree_robust,
)

log = structlog.get_logger(__name__)


async def test_builders(tmp_path: Path) -> None:
    """Build test.nix .dag via --builders."""
    async with asyncio.timeout(120):  # DAG builds can take longer
        test_nix = Path("test.nix")

        # 1. Backends for pynixd
        pynixd_local_path = STORE_PREFIX / "dag-builders-local"
        pynixd_builder_path = STORE_PREFIX / "dag-builders-builder"
        rmtree_robust(pynixd_local_path)
        rmtree_robust(pynixd_builder_path)

        pynixd_local = LocalSocketStore(
            id="pynixd-local",
            store_path=pynixd_local_path,
            **get_test_store_kwargs(),
        )
        pynixd_builder = LocalSocketStore(
            id="pynixd-builder",
            store_path=pynixd_builder_path,
            **get_test_store_kwargs(),
        )

        # 2. Local store for the 'nix' client to use.
        client_store_path = STORE_PREFIX / "client-store-dag-builders"
        rmtree_robust(pynixd_local_path)
        rmtree_robust(pynixd_builder_path)
        rmtree_robust(client_store_path)
        client_store_path.mkdir(parents=True, exist_ok=True)

        try:
            async with Server(
                local_store=pynixd_local, stores={"builder": pynixd_builder}, ssh_port=0
            ) as server:
                username = os.environ.get("USER", "root")

                # Direct URI with port as requested by user
                uri = f"ssh-ng://{username}@127.0.0.1:{server.port}"
                system = get_current_system()
                builder_spec = f"{uri} {system}"

                cmd = [
                    str(NIX_BIN),
                    "build",
                    "--store",
                    str(client_store_path),
                    "--builders",
                    builder_spec,
                    "--file",
                    str(test_nix),
                    "dag",
                    "--no-link",
                    "--print-out-paths",
                    "--max-jobs",
                    "0",
                    "--option",
                    "require-sigs",
                    "false",
                ]
                # Ensure SSH doesn't prompt for anything
                ssh_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

                rc, stdout, stderr, stdboth = await run_subproc(
                    cmd, env={"NIX_SSHOPTS": ssh_opts}
                )
                assert rc == 0, f"build failed:\n{stdboth}"
        finally:
            pass


async def test_store(tmp_path: Path) -> None:
    """Build test.nix .dag via --store."""
    async with asyncio.timeout(120):
        test_nix = Path("test.nix")

        pynixd_local_path = STORE_PREFIX / "dag-store-local"
        pynixd_builder_path = STORE_PREFIX / "dag-store-builder"
        rmtree_robust(pynixd_local_path)
        rmtree_robust(pynixd_builder_path)

        pynixd_local = LocalSocketStore(
            id="pynixd-local",
            store_path=pynixd_local_path,
            **get_test_store_kwargs(),
        )
        pynixd_builder = LocalSocketStore(
            id="pynixd-builder",
            store_path=pynixd_builder_path,
            **get_test_store_kwargs(),
        )

        try:
            with set_log_levels({"pynixd.op.AddToStore": logging.INFO}):
                async with Server(
                    local_store=pynixd_local,
                    stores={"builder": pynixd_builder},
                    ssh_port=0,
                ) as server:
                    username = os.environ.get("USER", "root")

                    uri = f"ssh-ng://{username}@127.0.0.1:{server.port}"

                    # Use --eval-store auto to evaluate against the system store,
                    # but build on the remote store via --store.
                    cmd = [
                        str(NIX_BIN),
                        "build",
                        "--eval-store",
                        "auto",
                        "--store",
                        uri,
                        "--file",
                        str(test_nix),
                        "dag",
                        "--no-link",
                        "--print-out-paths",
                    ]
                    rc, stdout, stderr, stdboth = await run_subproc(cmd)
                    assert rc == 0, f"build failed:\n{stdboth}"
        finally:
            pass
