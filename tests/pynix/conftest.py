from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
async def git_flake():
    with tempfile.TemporaryDirectory() as d:
        flake_dir = Path(d)
        (flake_dir / "flake.nix").write_text("""
        {
          outputs = { ... }: {
            hello = builtins.derivation {
              name = "test-hello";
              system = builtins.currentSystem;
              builder = "/bin/sh";
              args = [ "-c" "echo hi > $out" ];
            };
            greeting = "hi";
          };
        }
        """)
        for args in (
            ["git", "init"],
            ["git", "add", "flake.nix"],
            ["git", "commit", "-m", "init"],
        ):
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=flake_dir,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        yield flake_dir
