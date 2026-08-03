from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from nanopynix._ansi import strip_ansi
from pynix import Pynix
from tests.support.nix_environment import with_nixpkgs

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.nix_environment import NixTestEnvironment


async def test_show_file(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
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
    """,
            nixpkgs_path,
        )
    )
    cmd = Pynix.parse(
        ["derivation", "show", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()],
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
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
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
    """,
            nixpkgs_path,
        )
    )
    cmd = Pynix.parse(
        ["derivation", "show", "--file", str(nix_file), "--attr", "hello", *shared_nix_environment.pynix_store_args()],
    )
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    drv_path = next(iter(result))
    assert result[drv_path]["name"] == "nested-hello-1"


async def test_show_flake(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    cmd = Pynix.parse(
        ["derivation", "show", "--flake", f"{git_flake}#hello", *shared_nix_environment.pynix_store_args()]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    drv_path = next(iter(result))
    drv = result[drv_path]
    assert drv["name"] == "test-hello-1"
    assert "out" in drv["outputs"]


async def test_show_flake_greeting_is_not_derivation(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    cmd = Pynix.parse(
        ["derivation", "show", "--flake", f"{git_flake}#greeting", *shared_nix_environment.pynix_store_args()],
    )
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "value is not a derivation" in captured.err


async def test_show_missing_both_errors(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["derivation", "show"])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "either --file or --flake is required" in captured.err


async def test_show_both_file_and_flake_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{}")
    cmd = Pynix.parse(["derivation", "show", "--file", str(nix_file), "--flake", ".#hello"])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "--file and --flake are mutually exclusive" in captured.err


async def test_show_file_missing_attr_errors(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ present = 1; }")
    cmd = Pynix.parse(
        [
            "derivation",
            "show",
            "--file",
            str(nix_file),
            "--attr",
            "missing",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "attribute 'missing' not found" in strip_ansi(captured.err)


async def test_show_file_wrong_type_attr_errors(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ type = "not-a-derivation"; }')
    cmd = Pynix.parse(["derivation", "show", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "value at attribute path is not a derivation" in captured.err


async def test_show_file_non_string_drv_path_errors(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ type = "derivation"; drvPath = 123; }')
    cmd = Pynix.parse(["derivation", "show", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "failed to get derivation path" in captured.err
