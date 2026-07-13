from __future__ import annotations

import json
from pathlib import Path

import pytest

from pynix import Pynix


async def test_build_file_derivation(tmp_path, capsys):
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text("""
    let
      script = builtins.derivation {
        name = "pynix-build-script";
        system = builtins.currentSystem;
        builder = "/bin/sh";
        args = [
          "-c"
          "printf '%s\\n' 'echo built-from-file > $out' > $out"
        ];
      };
    in
      builtins.derivation {
        name = "pynix-build-file-test";
        system = builtins.currentSystem;
        builder = "/bin/sh";
        args = [ "${script}" ];
      }
    """)
    cmd = Pynix.parse(["build", "--file", str(nix_file)])

    await cmd.astart()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-file-test" in out_path
    assert Path(out_path).read_text() == "built-from-file\n"


async def test_build_file_derivation_attrpath(tmp_path, capsys):
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text("""
    {
      package = builtins.derivation {
        name = "pynix-build-attr-test";
        system = builtins.currentSystem;
        builder = "/bin/sh";
        args = [ "-c" "echo built-from-attr > $out" ];
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
            "--eval-store",
            "auto",
            "--print-build-logs",
        ]
    )

    await cmd.astart()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-attr-test" in out_path
    assert Path(out_path).read_text() == "built-from-attr\n"


async def test_build_flake_derivation(capsys, git_flake):
    cmd = Pynix.parse(["build", "--flake", f"{git_flake}#hello"])

    await cmd.astart()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "test-hello" in out_path
    assert Path(out_path).read_text() == "hi\n"


async def test_build_file_derivation_into_temporary_store(empty_store, tmp_path, capsys):
    nix_file = tmp_path / "temp-store-build.nix"
    nix_file.write_text("""
    builtins.derivation {
      name = "pynix-build-temp-store-test";
      system = builtins.currentSystem;
      builder = "/bin/sh";
      args = [ "-c" "echo built-in-temp-store > $out" ];
    }
    """)
    cmd = Pynix.parse(
        [
            "build",
            "--file",
            str(nix_file),
            "--store",
            empty_store["store_url"],
            "--eval-store",
            "auto",
        ]
    )

    await cmd.astart()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert out_path.startswith("/nix/store/")
    output_file = empty_store["store_root"] / out_path.removeprefix("/")
    assert output_file.read_text() == "built-in-temp-store\n"


async def test_build_missing_input_errors(capsys):
    cmd = Pynix.parse(["build"])

    with pytest.raises(SystemExit):
        await cmd.astart()

    captured = capsys.readouterr()
    assert "either --file or --flake is required" in captured.out
