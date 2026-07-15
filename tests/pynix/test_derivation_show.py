from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from pynix import Pynix


async def _init_git_flake(flake_dir: Path, nixpkgs_path: str) -> None:
    (flake_dir / "flake.nix").write_text(f"""
    {{
      inputs.nixpkgs.url = "path:{nixpkgs_path}";
      outputs = {{ nixpkgs, ... }}:
      let
        system = builtins.currentSystem;
        pkgs = nixpkgs.legacyPackages.${{system}};
      in
      {{
        hello = pkgs.stdenvNoCC.mkDerivation {{
          pname = "test-hello";
          version = "1";
          dontUnpack = true;
          installPhase = ''
            echo hi > "$out"
          '';
        }};
        greeting = "hi";
      }};
    }}
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


async def test_show_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("""
    let
      pkgs = import <nixpkgs> {};
    in
    pkgs.stdenvNoCC.mkDerivation {
      pname = "test-drv";
      version = "1";
      dontUnpack = true;
      installPhase = ''
        echo hi > "$out"
      '';
    }
    """)
    cmd = Pynix.parse(["derivation", "show", "--file", str(nix_file), "--store", "auto"])
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    drv_path = next(iter(result))
    drv = result[drv_path]
    assert drv["name"] == "test-drv-1"
    assert drv["system"] == "x86_64-linux"
    assert "out" in drv["outputs"]


async def test_show_file_with_attr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("""
    let
      pkgs = import <nixpkgs> {};
    in
    {
      hello = pkgs.stdenvNoCC.mkDerivation {
        pname = "nested-hello";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo hi > "$out"
        '';
      };
    }
    """)
    cmd = Pynix.parse(["derivation", "show", "--file", str(nix_file), "--attr", "hello"])
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    drv_path = next(iter(result))
    assert result[drv_path]["name"] == "nested-hello-1"


async def test_show_flake(capsys: pytest.CaptureFixture[str], nixpkgs_path: str) -> None:
    with tempfile.TemporaryDirectory() as d:
        flake_dir = Path(d)
        await _init_git_flake(flake_dir, nixpkgs_path)
        cmd = Pynix.parse(["derivation", "show", "--flake", f"{flake_dir}#hello"])
        await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    drv_path = next(iter(result))
    drv = result[drv_path]
    assert drv["name"] == "test-hello-1"
    assert "out" in drv["outputs"]


async def test_show_flake_greeting_is_not_derivation(capsys: pytest.CaptureFixture[str], nixpkgs_path: str) -> None:
    with tempfile.TemporaryDirectory() as d:
        flake_dir = Path(d)
        await _init_git_flake(flake_dir, nixpkgs_path)
        cmd = Pynix.parse(["derivation", "show", "--flake", f"{flake_dir}#greeting"])
        with pytest.raises(SystemExit):
            await cmd.astart()
    captured = capsys.readouterr()
    assert "value is not a derivation" in captured.out


async def test_show_missing_both_errors(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["derivation", "show"])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "either --file or --flake is required" in captured.out


async def test_show_both_file_and_flake_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{}")
    cmd = Pynix.parse(["derivation", "show", "--file", str(nix_file), "--flake", ".#hello"])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "--file and --flake are mutually exclusive" in captured.out
