# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportMissingParameterType=false, reportUnknownParameterType=false
# nanopynix / pynix are C++ nanobind extensions without type stubs.
# Variable types cascade from unknown member access; capsys fixture lacks stubs.

from __future__ import annotations

import pytest
from nanopynix_proto.nix.store import GetBuildLogRequest

from pynix import Pynix


async def test_nanopynix_store_get_build_log_from_populated_store(populated_store: dict[str, str]):
    import nanopynix

    async with nanopynix.Session() as nix, nix.store(populated_store["store_url"]) as store:
        response = await store.get_build_log(GetBuildLogRequest(path=populated_store["log_path"]))

    assert response.log is not None
    assert "pynix-log-line" in response.log


async def test_pynix_log_prints_build_log_from_populated_store(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["log", populated_store["log_path"], "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    assert "pynix-log-line" in captured.out
