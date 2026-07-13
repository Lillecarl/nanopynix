from __future__ import annotations

import json

import pytest
from anyio import Path

from pynix import Pynix


async def test_build_file_derivation(tmp_path, capsys):
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text("""
    let
      pkgs = import <nixpkgs> {};
    in
      pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-file-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-from-file > "$out"
        '';
      }
    """)
    cmd = Pynix.parse(["build", "--file", str(nix_file)])

    await cmd.astart()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-file-test" in out_path
    assert await Path(out_path).read_text() == "built-from-file\n"


async def test_build_file_derivation_attrpath(tmp_path, capsys):
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text("""
    let
      pkgs = import <nixpkgs> {};
    in
    {
      package = pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-attr-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-from-attr > "$out"
        '';
      };
    }
    """)
    cmd = Pynix.parse(
        [
            "build",
            "--file",
            str(nix_file),
            "--attrpath",
            "package",
            "--store",
            "auto",
            "--print-build-logs",
        ]
    )

    await cmd.astart()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-attr-test" in out_path
    assert await Path(out_path).read_text() == "built-from-attr\n"


async def test_build_file_auto_calls_defaulted_lambda_before_attrpath(tmp_path, capsys):
    nix_file = tmp_path / "default.nix"
    nix_file.write_text("""
    { pkgs ? import <nixpkgs> {}, name ? "pynix-build-autocall-test" }:
    {
      package = pkgs.stdenvNoCC.mkDerivation {
        pname = name;
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-from-autocall > "$out"
        '';
      };
    }
    """)
    cmd = Pynix.parse(["build", "--file", str(nix_file), "--attrpath", "package"])

    await cmd.astart()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-autocall-test" in out_path
    assert await Path(out_path).read_text() == "built-from-autocall\n"


async def test_build_missing_attrpath_errors_before_build(tmp_path, capsys):
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text("""
    let
      pkgs = import <nixpkgs> {};
    in
    {
      package = pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-attr-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo should-not-build > "$out"
        '';
      };
    }
    """)
    cmd = Pynix.parse(["build", "--file", str(nix_file), "--attrpath", "missing"])

    with pytest.raises(SystemExit):
        await cmd.astart()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "attribute 'missing' not found" in captured.err


async def test_build_flake_derivation(capsys, git_flake):
    cmd = Pynix.parse(["build", "--flake", f"{git_flake}#hello"])

    await cmd.astart()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "test-hello" in out_path
    assert await Path(out_path).read_text() == "hi\n"


async def test_build_missing_input_errors(capsys):
    cmd = Pynix.parse(["build"])

    with pytest.raises(SystemExit):
        await cmd.astart()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "either --file or --flake is required" in captured.err
