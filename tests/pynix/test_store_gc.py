from __future__ import annotations

import json

import pytest

from pynix import Pynix


@pytest.mark.anyio
async def test_print_roots(capsys):
    cmd = Pynix.parse(["store", "gc", "print-roots"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "roots" in data
    assert isinstance(data["roots"], list)
    for root in data["roots"]:
        assert "link" in root
        assert "path" in root
        assert root["path"].startswith("/nix/store/")


@pytest.mark.anyio
async def test_print_alive(capsys):
    cmd = Pynix.parse(["store", "gc", "print-alive"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "paths" in data
    assert isinstance(data["paths"], list)
    for path in data["paths"]:
        assert path.startswith("/nix/store/")


@pytest.mark.anyio
async def test_print_dead_dry_run(capsys):
    cmd = Pynix.parse(["store", "gc", "print-dead"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "paths" in data
    assert "bytesFreed" in data
    assert isinstance(data["paths"], list)
    assert isinstance(data["bytesFreed"], int)


def test_print_dead_help(capsys):
    with pytest.raises(SystemExit) as exc_info:
        Pynix.parse(["store", "gc", "print-dead", "--help"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--rip" in captured.out
