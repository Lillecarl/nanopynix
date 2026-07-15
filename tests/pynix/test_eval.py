from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from pynix import Pynix

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _parse_json_output(out: str) -> object:
    """Extract the JSON portion from captured stdout, skipping structlog lines."""
    _structlog = re.compile(r"^\d{4}-\d{2}-\d{2}\s")
    lines = [line for line in out.splitlines() if not _structlog.match(line)]
    return json.loads("".join(lines))


async def test_eval_expr(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["eval", "--expr", "1 + 1", "--store", "auto"])
    await cmd.astart()
    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == 2


async def test_eval_string(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["eval", "--expr", '"hello"'])
    await cmd.astart()
    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == "hello"


async def test_eval_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; b = true; c = [ 1 2 3 ]; }")
    cmd = Pynix.parse(["eval", "--file", str(nix_file)])
    await cmd.astart()
    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == {"a": 1, "b": True, "c": [1, 2, 3]}


async def test_eval_file_attr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ nested = { answer = 42; }; }")
    cmd = Pynix.parse(["eval", "--file", str(nix_file), "--attr", "nested"])

    await cmd.astart()

    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == {"answer": 42}


async def test_eval_json_sorted_keys(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["eval", "--expr", "{ z = 1; a = 2; }"])
    await cmd.astart()
    captured = capsys.readouterr()
    result = _parse_json_output(captured.out)
    assert json.dumps(result, sort_keys=True, indent=2) + "\n" in captured.out
