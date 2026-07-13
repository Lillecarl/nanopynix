from __future__ import annotations

import json

import pytest

from pynix import Pynix


async def test_config_show(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["config", "show"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, dict)
    assert "store" in data


async def test_config_show_one_setting(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["config", "show", "--setting", "store"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert set(data) == {"store"}
    assert isinstance(data["store"], str)


async def test_config_check(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["config", "check"])
    await cmd.astart()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True}


async def test_config_current_system(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["config", "current-system"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert set(data) == {"currentSystem"}
    assert isinstance(data["currentSystem"], str)
    assert data["currentSystem"]
