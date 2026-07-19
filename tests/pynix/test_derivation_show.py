from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pynix import Pynix

if TYPE_CHECKING:
    from tests.support.nix_environment import NixTestEnvironment


def _with_nixpkgs(source: str, nixpkgs_path: str) -> str:
    return source.replace("<nixpkgs>", nixpkgs_path)


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


async def test_show_file(
    isolated_nix_environment: NixTestEnvironment, nixpkgs_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text(_with_nixpkgs("""
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
    """, nixpkgs_path))
    cmd = Pynix.parse(
        ["derivation", "show", "--file", str(nix_file), *isolated_nix_environment.pynix_store_args()]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    drv_path = next(iter(result))
    drv = result[drv_path]
    assert drv["name"] == "test-drv-1"
    assert drv["system"] == "x86_64-linux"
    assert "out" in drv["outputs"]


async def test_show_file_with_attr(
    isolated_nix_environment: NixTestEnvironment, nixpkgs_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text(_with_nixpkgs("""
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
    """, nixpkgs_path))
    cmd = Pynix.parse(
        ["derivation", "show", "--file", str(nix_file), "--attr", "hello", *isolated_nix_environment.pynix_store_args()]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    drv_path = next(iter(result))
    assert result[drv_path]["name"] == "nested-hello-1"


async def test_show_flake(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str], nixpkgs_path: str
) -> None:
    flake_dir = tmp_path / "flake"
    flake_dir.mkdir()
    await _init_git_flake(flake_dir, nixpkgs_path)
    cmd = Pynix.parse(["derivation", "show", "--flake", f"{flake_dir}#hello", *isolated_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    drv_path = next(iter(result))
    drv = result[drv_path]
    assert drv["name"] == "test-hello-1"
    assert "out" in drv["outputs"]


async def test_show_flake_greeting_is_not_derivation(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str], nixpkgs_path: str
) -> None:
    flake_dir = tmp_path / "flake"
    flake_dir.mkdir()
    await _init_git_flake(flake_dir, nixpkgs_path)
    cmd = Pynix.parse(
        ["derivation", "show", "--flake", f"{flake_dir}#greeting", *isolated_nix_environment.pynix_store_args()]
    )
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


async def test_show_file_missing_attr_errors(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ present = 1; }")
    cmd = Pynix.parse(
        ["derivation", "show", "--file", str(nix_file), "--attr", "missing", *isolated_nix_environment.pynix_store_args()]
    )
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "attribute 'missing' not found" in captured.out


async def test_show_file_wrong_type_attr_errors(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ type = "not-a-derivation"; }')
    cmd = Pynix.parse(["derivation", "show", "--file", str(nix_file), *isolated_nix_environment.pynix_store_args()])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "value at attribute path is not a derivation" in captured.out


async def test_show_file_non_string_drv_path_errors(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ type = "derivation"; drvPath = 123; }')
    cmd = Pynix.parse(["derivation", "show", "--file", str(nix_file), *isolated_nix_environment.pynix_store_args()])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "failed to get derivation path" in captured.out
