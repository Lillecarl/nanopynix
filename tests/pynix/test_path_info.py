from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from pynix import Pynix

if TYPE_CHECKING:
    from nanopynix.models import StorePath
    from tests.support.nix_environment import NixTestEnvironment


async def test_path_info(
    isolated_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = str(seeded_store_path)

    cmd = Pynix.parse(["path-info", path, *isolated_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["path"] == path
    assert isinstance(result["narHash"], str)
    assert isinstance(result["narSize"], int)
    assert result["narSize"] > 0
    assert isinstance(result["references"], list)


async def test_path_info_nonexistent(
    isolated_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    cmd = Pynix.parse(
        ["path-info", "/nix/store/deadbeef-nonexistent", *isolated_nix_environment.pynix_store_args()]
    )
    with pytest.raises(SystemExit):
        await cmd.astart()
    captured = capsys.readouterr()
    assert "Error" in captured.out
