from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from pynix import Pynix


async def _init_git_flake(flake_dir: Path) -> None:
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


async def test_show_file(tmp_path, capsys):
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("""
    builtins.derivation {
      name = "test-drv";
      system = builtins.currentSystem;
      builder = "/bin/sh";
      args = [ "-c" "echo hi > $out" ];
    }
    """)
    cmd = Pynix.parse(["derivation", "show", "--file", str(nix_file)])
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    drv_path = next(iter(result))
    drv = result[drv_path]
    assert drv["name"] == "test-drv"
    assert drv["builder"] == "/bin/sh"
    assert drv["system"] == "x86_64-linux"
    assert drv["args"] == ["-c", "echo hi > $out"]
    assert "out" in drv["outputs"]


async def test_show_file_with_attrpath(tmp_path, capsys):
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("""
    {
      hello = builtins.derivation {
        name = "nested-hello";
        system = builtins.currentSystem;
        builder = "/bin/sh";
        args = [ "-c" "echo hi > $out" ];
      };
    }
    """)
    cmd = Pynix.parse(["derivation", "show", "--file", str(nix_file), "--attrpath", "hello"])
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    drv_path = next(iter(result))
    assert result[drv_path]["name"] == "nested-hello"


async def test_show_flake(capsys):
    with tempfile.TemporaryDirectory() as d:
        flake_dir = Path(d)
        await _init_git_flake(flake_dir)
        cmd = Pynix.parse(["derivation", "show", "--flake", f"{flake_dir}#hello"])
        await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    drv_path = next(iter(result))
    drv = result[drv_path]
    assert drv["name"] == "test-hello"
    assert drv["builder"] == "/bin/sh"
    assert "out" in drv["outputs"]


async def test_show_flake_greeting_is_not_derivation(capsys):
    with tempfile.TemporaryDirectory() as d:
        flake_dir = Path(d)
        await _init_git_flake(flake_dir)
        cmd = Pynix.parse(["derivation", "show", "--flake", f"{flake_dir}#greeting"])
        with pytest.raises(SystemExit):
            await cmd.astart()
    captured = capsys.readouterr()
    assert "value is not a derivation" in captured.out


async def test_show_missing_both_errors(capsys):
    cmd = Pynix.parse(["derivation", "show"])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "either --file or --flake is required" in captured.out


async def test_show_both_file_and_flake_errors(tmp_path, capsys):
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{}")
    cmd = Pynix.parse(["derivation", "show", "--file", str(nix_file), "--flake", ".#hello"])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "--file and --flake are mutually exclusive" in captured.out
