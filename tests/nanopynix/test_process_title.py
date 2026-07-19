"""Process-title behavior for nanopynix managers and workers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import nanopynix._process_title as process_title
import nanopynix.rpc.client.session as nix_module
from nanopynix.rpc.client.session import Session
from nanopynix.rpc.client.store import StoreHandle


def test_manager_title_uses_current_project(monkeypatch: pytest.MonkeyPatch) -> None:
    titles: list[str] = []
    monkeypatch.setattr(process_title, "setproctitle", titles.append)
    monkeypatch.setattr(process_title, "_manager_project_name", "nanopynix")
    process_title.set_manager_title("pynix")

    assert titles == ["pynix (manager)"]


def test_manager_title_selection_is_used_by_later_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    titles: list[str] = []
    monkeypatch.setattr(process_title, "setproctitle", titles.append)
    monkeypatch.setattr(process_title, "_manager_project_name", "nanopynix")

    process_title.set_manager_title("pynix")
    process_title.set_manager_title()

    assert titles == ["pynix (manager)", "pynix (manager)"]


def test_worker_title_uses_nanopynix_and_two_word_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    titles: list[str] = []
    word_counts: list[int] = []
    monkeypatch.setattr(process_title, "setproctitle", titles.append)

    def generate_slug(words: int) -> str:
        word_counts.append(words)
        return "quiet-otter"

    monkeypatch.setattr(process_title, "generate_slug", generate_slug)

    process_title.set_worker_title()

    assert word_counts == [2]
    assert titles == ["nanopynix (quiet-otter)"]


def test_session_sets_manager_title(monkeypatch: pytest.MonkeyPatch) -> None:
    manager_titles: list[None] = []
    monkeypatch.setattr(nix_module, "set_manager_title", lambda: manager_titles.append(None))

    Session()

    assert manager_titles == [None]


@pytest.mark.anyio
async def test_store_session_opens_store_with_its_uri() -> None:
    pool = MagicMock()
    pool._worker_stub.open_store = AsyncMock(return_value=SimpleNamespace(store_handle=42))

    async def invoke(method: object, request: object, *, timeout: float) -> object:  # noqa: ASYNC109 -- mock implementing WorkerClient.invoke interface
        del timeout
        return await method(request)  # type: ignore[operator] -- test double receives a generated RPC method

    pool.invoke = invoke
    store = StoreHandle(pool, "local?root=/tmp/test-store", "session-id")

    await store.open()

    request = pool._worker_stub.open_store.call_args.args[0]
    assert request.uri == "local?root=/tmp/test-store"
