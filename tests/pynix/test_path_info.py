from __future__ import annotations

import json

import pytest

import nanopynix
from pynix import Pynix


async def test_path_info(capsys: pytest.CaptureFixture[str]):
    # A literal hash-pinned store path (e.g. a specific bash build) isn't
    # guaranteed to be valid in every store — it depends on exactly which
    # nixpkgs revision built the current environment's closure. Query a path
    # that is actually valid in *this* store instead.
    async with nanopynix.Session() as nix, nix.store() as store:
        valid_paths = await store.query_all_valid_paths()
    path = str(valid_paths[0])

    cmd = Pynix.parse(["path-info", path, "--store", "auto"])
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["path"] == path
    assert isinstance(result["narHash"], str)
    assert isinstance(result["narSize"], int)
    assert result["narSize"] > 0
    assert isinstance(result["references"], list)


async def test_path_info_nonexistent(capsys: pytest.CaptureFixture[str]):
    cmd = Pynix.parse(["path-info", "/nix/store/deadbeef-nonexistent"])
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "Error" in captured.out
