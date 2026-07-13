from __future__ import annotations

import json
import os

import pytest

from pynix import Pynix


def _bash_store_path() -> str:
    return os.readlink("/run/current-system/sw/bin/bash").split("/nix/store/")[1].split("/")[0]


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


async def test_print_alive(capsys):
    cmd = Pynix.parse(["store", "gc", "print-alive"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "paths" in data
    assert isinstance(data["paths"], list)
    for path in data["paths"]:
        assert path.startswith("/nix/store/")


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


async def test_path_from_hash_part(capsys):
    store_path = _bash_store_path()
    hash_part = store_path.split("-", 1)[0]
    cmd = Pynix.parse(["store", "path-from-hash-part", hash_part])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["path"] == f"/nix/store/{store_path}"


async def test_ensure_path(capsys):
    store_path = f"/nix/store/{_bash_store_path()}"
    cmd = Pynix.parse(["store", "ensure-path", store_path])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": store_path, "valid": True}


async def test_optimise_empty_local_store(tmp_path, capsys):
    cmd = Pynix.parse(["store", "optimise", "--store", f"local?root={tmp_path}"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"optimised": True}


async def test_verify_empty_local_store(tmp_path, capsys):
    cmd = Pynix.parse(["store", "verify", "--store", f"local?root={tmp_path}"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"errors": False}
