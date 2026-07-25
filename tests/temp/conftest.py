"""Fixtures for the TEMPORARY error-boundary diagnostic matrix.

``nixpkgs_path`` is duplicated from ``tests/pynix/conftest.py`` rather than
imported: this whole directory is scaffolding for CIP3 items 2-3 and is meant
to be deleted wholesale, so it deliberately takes no dependency on another
suite's conftest.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest


async def _run(*args: str) -> bytes:
    process = await anyio.open_process(args)
    async with process:
        stdout = b""
        if process.stdout is not None:
            stdout = await process.stdout.receive()
        await process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"{args!r} exited {process.returncode}")
        return stdout


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
async def nixpkgs_path(repo_root: Path) -> str:
    stdout = await _run("nix", "eval", "--impure", "--raw", "--file", str(repo_root), "pkgs.path")
    return stdout.decode().strip()
