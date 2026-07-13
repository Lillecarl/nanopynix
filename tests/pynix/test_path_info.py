from __future__ import annotations

import json

import pytest

from pynix import Pynix


async def test_path_info(capsys):
    cmd = Pynix.parse(["path-info", "/nix/store/zh1ijdhb6gng1509b1zrilb6xlzx60j6-bash-5.3p9", "--store", "auto"])
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["path"] == "/nix/store/zh1ijdhb6gng1509b1zrilb6xlzx60j6-bash-5.3p9"
    assert isinstance(result["narHash"], str)
    assert isinstance(result["narSize"], int)
    assert result["narSize"] > 0
    assert isinstance(result["references"], list)
    assert "/nix/store/zh1ijdhb6gng1509b1zrilb6xlzx60j6-bash-5.3p9" in result["references"]


async def test_path_info_nonexistent(capsys):
    cmd = Pynix.parse(["path-info", "/nix/store/deadbeef-nonexistent"])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "Error" in captured.out
