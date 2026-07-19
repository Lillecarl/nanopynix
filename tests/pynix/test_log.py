# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportMissingParameterType=false, reportUnknownParameterType=false
# nanopynix / pynix are C++ nanobind extensions without type stubs.
# Variable types cascade from unknown member access; capsys fixture lacks stubs.

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pynix.log as log_module
import pytest
from nanopynix_proto.nix.store import GetBuildLogRequest

from pynix import Pynix

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from tests.support.nix_environment import NixTestEnvironment


async def test_nanopynix_store_get_build_log_from_populated_store(
    populated_store: dict[str, str],
    shared_nix_environment: NixTestEnvironment,
) -> None:
    async with shared_nix_environment.rpc_session() as nix, nix.store() as store:
        response = await store.rpc.get_build_log(GetBuildLogRequest(path=populated_store["log_path"]))

    assert response.log is not None
    assert "pynix-log-line" in response.log


async def test_pynix_log_prints_build_log_from_populated_store(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    cmd = Pynix.parse(["log", populated_store["log_path"], "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    assert "pynix-log-line" in captured.out


async def test_pynix_log_errors_when_build_log_unavailable(
    isolated_nix_environment: NixTestEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "/nix/store/00000000000000000000000000000000-no-log"
    monkeypatch.setattr(log_module, "prepare_sys_path", lambda: None)
    monkeypatch.setattr(log_module, "forward_nix_logs", _noop_forward_nix_logs)
    monkeypatch.setattr(log_module, "nanopynix", SimpleNamespace(Session=_FakeSession))

    cmd = Pynix.parse(["log", path, *isolated_nix_environment.pynix_store_args()])
    with pytest.raises(SystemExit, match=r"build log of .* is not available"):
        await cmd.astart()


@asynccontextmanager
async def _noop_forward_nix_logs(session: Any, *, print_build_logs: bool = False) -> AsyncGenerator[None]:  # noqa: ARG001 -- matches real forward_nix_logs signature
    yield


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def store(self, uri: str) -> _FakeStore:
        return _FakeStore()

    async def log_stream(self) -> AsyncIterator[None]:
        if False:
            yield None


class _FakeStore:
    async def __aenter__(self) -> _FakeStore:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get_build_log(self, path: str) -> str | None:
        return None
