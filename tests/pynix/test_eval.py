from __future__ import annotations

import io
import json
import re
from typing import TYPE_CHECKING

import pytest

from pynix import Pynix

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.nix_environment import NixTestEnvironment


def _parse_json_output(out: str) -> object:
    """Extract the JSON portion from captured stdout, skipping structlog lines."""
    _structlog = re.compile(r"^\d{4}-\d{2}-\d{2}\s")
    lines = [line for line in out.splitlines() if not _structlog.match(line)]
    return json.loads("".join(lines))


async def test_eval_expr(
    isolated_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    cmd = Pynix.parse(["eval", "--expr", "1 + 1", *isolated_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == 2


async def test_eval_string(
    isolated_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    cmd = Pynix.parse(["eval", "--expr", '"hello"', *isolated_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == "hello"


async def test_eval_file(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; b = true; c = [ 1 2 3 ]; }")
    cmd = Pynix.parse(["eval", "--file", str(nix_file), *isolated_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == {"a": 1, "b": True, "c": [1, 2, 3]}


async def test_eval_file_attr(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ nested = { answer = 42; }; }")
    cmd = Pynix.parse(
        ["eval", "--file", str(nix_file), "--attr", "nested", *isolated_nix_environment.pynix_store_args()]
    )

    await cmd.astart()

    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == {"answer": 42}


async def test_eval_json_sorted_keys(
    isolated_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    cmd = Pynix.parse(["eval", "--expr", "{ z = 1; a = 2; }", *isolated_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    result = _parse_json_output(captured.out)
    assert json.dumps(result, sort_keys=True, indent=2) + "\n" in captured.out


async def test_eval_attr_without_file_or_flake_errors(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["eval", "--expr", "1", "--attr", "x"])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "--attr requires --file or --flake" in captured.out


async def test_eval_expr_combined_with_file_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("1")
    cmd = Pynix.parse(["eval", "--expr", "1", "--file", str(nix_file)])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "expression argument cannot be combined with --file or --flake" in captured.out


async def test_eval_reads_expression_from_stdin(
    isolated_nix_environment: NixTestEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("21 + 21"))
    cmd = Pynix.parse(["eval", *isolated_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == 42


async def test_eval_file_missing_attr_errors(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ present = 1; }")
    cmd = Pynix.parse(
        ["eval", "--file", str(nix_file), "--attr", "missing", *isolated_nix_environment.pynix_store_args()]
    )
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "attribute 'missing' not found" in captured.out
