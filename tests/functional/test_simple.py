"""Simple end-to-end build tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pyinstrument
import structlog
from pyinstrument.renderers import ConsoleRenderer
import asyncio
import os

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


async def test_builders(test_log_dir: Path, tmp_path: Path) -> None:
    """Build test.nix .simple via --builders."""
    async with asyncio.timeout(60):
        test_nix = Path("test.nix")

        # 1. Backends for pynixd
        pynixd_local_path = STORE_PREFIX / "pynixd-local-builders"
        pynixd_builder_path = STORE_PREFIX / "pynixd-builder-builders"
        client_store_path = STORE_PREFIX / "client-store-builders"
        rmtree_robust(pynixd_local_path)
        rmtree_robust(pynixd_builder_path)
        rmtree_robust(client_store_path)

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

        profiler = pyinstrument.Profiler(async_mode="enabled")
        profiler.start()

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
                    "simple",
                    "--no-link",
                    "--print-out-paths",
                    "--max-jobs",
                    "0",
                    "--option",
                    "require-sigs",
                    "false",
                ]
                rc, stdout, stderr, stdboth = await run_subproc(
                    cmd, env={"NIX_STATE_DIR": str(client_store_path / "var/nix")}
                )
                assert rc == 0, f"build failed:\n{stdboth}"
        finally:
            profiler.stop()
            session = profiler.last_session
            if session:
                renderer = ConsoleRenderer(unicode=True, color=False, show_all=True)
                profile_path = test_log_dir / "pyinstrument-builders"
                with open(profile_path, "w") as f:
                    f.write(renderer.render(session))


async def test_store(test_log_dir: Path, tmp_path: Path) -> None:
    """Build test.nix .simple via --eval-store."""
    async with asyncio.timeout(60):
        test_nix = Path("test.nix")

        pynixd_local_path = STORE_PREFIX / "pynixd-local-store"
        pynixd_builder_path = STORE_PREFIX / "pynixd-builder-store"
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

        profiler = pyinstrument.Profiler(async_mode="enabled")
        profiler.start()

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
                        "simple",
                        "--no-link",
                        "--print-out-paths",
                    ]
                    rc, stdout, stderr, stdboth = await run_subproc(cmd)
                    assert rc == 0, f"build failed:\n{stdboth}"
        finally:
            profiler.stop()
            session = profiler.last_session
            if session:
                renderer = ConsoleRenderer(unicode=True, color=False, show_all=True)
                profile_path = test_log_dir / "pyinstrument-store"
                with open(profile_path, "w") as f:
                    f.write(renderer.render(session))
