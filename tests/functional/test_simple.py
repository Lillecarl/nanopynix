"""Simple end-to-end build tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pyinstrument
import structlog
from pyinstrument.renderers import ConsoleRenderer
import asyncio

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


async def test_builders(test_log_dir: Path) -> None:
    """Build test.nix .simple via --builders."""
    test_nix = Path("test.nix")
    local_store = LocalSocketStore(
        id="local",
        store_path=STORE_PREFIX / "local",
        **get_test_store_kwargs(),
    )
    builder_store = LocalSocketStore(
        id="builder",
        store_path=STORE_PREFIX / "builder",
        **get_test_store_kwargs(),
    )

    profiler = pyinstrument.Profiler(async_mode="enabled")
    profiler.start()

    try:
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
                "--max-jobs",
                "0",
            ]
            rc, stdout, stderr, _ = await run_subproc(cmd)
            assert rc == 0, f"""build failed:
{stderr}"""
    finally:
        profiler.stop()
        session = profiler.last_session
        if session:
            renderer = ConsoleRenderer(unicode=True, color=False, show_all=True)
            profile_path = test_log_dir / "pyinstrument"
            with open(profile_path, "w") as f:
                f.write(renderer.render(session))


async def test_store(test_log_dir: Path) -> None:
    """Build test.nix .simple via --store."""
    test_nix = Path("test.nix")
    local_store = LocalSocketStore(
        id="local",
        store_path=STORE_PREFIX / "local-store",
        **get_test_store_kwargs(),
    )
    builder_store = LocalSocketStore(
        id="builder",
        store_path=STORE_PREFIX / "builder-store",
        **get_test_store_kwargs(),
    )

    profiler = pyinstrument.Profiler(async_mode="enabled")
    profiler.start()

    # AddToStore is muted to INFO because it produces ~4500 DEBUG log lines
    # (one per path being added). This is 100% confirmed working — the missing
    # DEBUG output is NOT the cause of any test failure. Do NOT remove this
    # silencing unless you want to drown the AI in thousands of log lines.
    try:
        async with asyncio.timeout(60):
            with set_log_levels({"pynixd.op.AddToStore": logging.INFO}):
                async with Server(
                    local_store=local_store,
                    stores={"builder": builder_store},
                    ssh_port=0,
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
                    rc, stdout, stderr, stdboth = await run_subproc(cmd)
                    assert rc == 0, f"build failed:\n{stdboth}"
    finally:
        profiler.stop()
        session = profiler.last_session
        if session:
            renderer = ConsoleRenderer(unicode=True, color=False, show_all=True)
            profile_path = test_log_dir / "pyinstrument"
            with open(profile_path, "w") as f:
                f.write(renderer.render(session))
