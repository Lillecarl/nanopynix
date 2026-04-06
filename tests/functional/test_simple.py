"""Simple end-to-end build tests."""

from __future__ import annotations

import logging
from pathlib import Path

import structlog

from pynixd import Server
from pynixd.instance import NixImplementation
from pynixd.store import LocalSocketStore
from tests.conftest import NIX_BIN, STORE_PREFIX, run_captured, set_log_levels

log = structlog.get_logger(__name__)


async def test_builders() -> None:
    """Build test.nix .simple via --builders."""
    test_nix = Path("test.nix")
    local_store = LocalSocketStore(
        id="local", store_path=STORE_PREFIX / "local", nix_bin=NIX_BIN
    )
    builder_store = LocalSocketStore(
        id="builder", store_path=STORE_PREFIX / "builder", nix_bin=NIX_BIN
    )

    async with Server(
        local_store=local_store, stores={"builder": builder_store}, ssh_port=0
    ) as server:
        uri = server.builder_uri(implementation=NixImplementation.NIX, max_jobs=1)
        cmd = [
            str(NIX_BIN),
            "build",
            "--builders",
            uri,
            "--file",
            str(test_nix),
            "simple",
            "--no-link",
            "--print-out-paths",
        ]
        rc, stdout, stderr = await run_captured(cmd)
        assert rc == 0, f"build failed:\n{stderr}"


async def test_store() -> None:
    """Build test.nix .simple via --store."""
    test_nix = Path("test.nix")
    local_store = LocalSocketStore(
        id="local", store_path=STORE_PREFIX / "local", nix_bin=NIX_BIN
    )
    builder_store = LocalSocketStore(
        id="builder", store_path=STORE_PREFIX / "builder", nix_bin=NIX_BIN
    )

    # AddToStore is muted to INFO because it produces ~4500 DEBUG log lines
    # (one per path being added). This is 100% confirmed working — the missing
    # DEBUG output is NOT the cause of any test failure. Do NOT remove this
    # silencing unless you want to drown the AI in thousands of log lines.
    with set_log_levels({"pynixd.op.AddToStore": logging.INFO}):
        log.info(
            "IMPORTANT: pynixd.op.AddToStore is configured to INFO "
            "so you won't see it here unless it errors out"
        )
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
                "simple",
                "--no-link",
                "--print-out-paths",
            ]
            rc, stdout, stderr = await run_captured(cmd)
            assert rc == 0, f"build failed:\n{stderr}"
