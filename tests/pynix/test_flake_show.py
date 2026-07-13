from __future__ import annotations

from pynix import Pynix


async def test_flake_show_root(capsys, git_flake):
    cmd = Pynix.parse(["flake", "show", str(git_flake)])
    await cmd.astart()
    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "greeting" in captured.out


async def test_flake_show_with_hash_attrpath(capsys, git_flake):
    cmd = Pynix.parse(["flake", "show", f"{git_flake}#hello"])
    await cmd.astart()
    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "greeting" not in captured.out
