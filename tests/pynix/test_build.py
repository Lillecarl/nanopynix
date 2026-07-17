from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from anyio import Path as AnyioPath
from strip_ansi import strip_ansi  # type: ignore[reportMissingTypeStubs] -- strip_ansi has no PEP 561 stubs

from pynix import Pynix

if TYPE_CHECKING:
    from pathlib import Path


async def test_build_file_derivation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    assert await AnyioPath(out_path).read_text() == "built-from-file\n"


async def test_build_file_derivation_attr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
            "--attr",
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
    assert await AnyioPath(out_path).read_text() == "built-from-attr\n"


async def test_build_file_auto_calls_defaulted_lambda_before_attr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    cmd = Pynix.parse(["build", "--file", str(nix_file), "--attr", "package"])

    await cmd.astart()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-autocall-test" in out_path
    assert await AnyioPath(out_path).read_text() == "built-from-autocall\n"


async def test_build_missing_attr_errors_before_build(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    cmd = Pynix.parse(["build", "--file", str(nix_file), "--attr", "missing"])

    with pytest.raises(SystemExit):
        await cmd.astart()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "attribute 'missing' not found" in strip_ansi(captured.err)


async def test_build_flake_derivation(capsys: pytest.CaptureFixture[str], git_flake: Path) -> None:
    cmd = Pynix.parse(["build", "--flake", f"{git_flake}#hello"])

    await cmd.astart()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "test-hello" in out_path
    assert await AnyioPath(out_path).read_text() == "hi\n"


async def test_build_missing_input_errors(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["build"])

    with pytest.raises(SystemExit):
        await cmd.astart()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "either --file or --flake is required" in captured.err


async def test_build_with_separate_eval_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text("""
    let
      pkgs = import <nixpkgs> {};
    in
      pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-eval-store-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-with-eval-store > "$out"
        '';
      }
    """)
    cmd = Pynix.parse(["build", "--file", str(nix_file), "--eval-store", "auto"])

    await cmd.astart()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-eval-store-test" in out_path
    assert await AnyioPath(out_path).read_text() == "built-with-eval-store\n"


async def test_build_update_fod_dry_run_reports_local_fetchurl_diff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = tmp_path / "payload"
    payload.write_text("fixed-output payload\n")
    nix_file = tmp_path / "source.nix"
    source = f'builtins.fetchurl {{ url = "file://{payload}"; sha256 = ""; }}\n'
    nix_file.write_text(source)
    cmd = Pynix.parse(["build", "--file", str(nix_file), "--update-fod", "--dry-run"])

    await cmd.astart()

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"dryRun": True, "outputs": {}, "updatedFods": 1}
    assert "-builtins.fetchurl" in strip_ansi(captured.err)
    assert "+builtins.fetchurl" in strip_ansi(captured.err)
    assert nix_file.read_text() == source


async def test_build_update_fod_rewrites_and_rebuilds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        """with import <nixpkgs> {};
runCommand "payload" {
  outputHash = "";
  outputHashAlgo = "sha256";
  outputHashMode = "flat";
} "printf '%s\\n' fixed-output-payload > $out"
"""
    )
    cmd = Pynix.parse(["build", "--file", str(nix_file), "--update-fod"])

    await cmd.astart()

    data = json.loads(capsys.readouterr().out)
    assert data["updatedFods"] == 1
    assert data["outputs"]["out"]
    assert 'outputHash = "sha256-' in nix_file.read_text()
