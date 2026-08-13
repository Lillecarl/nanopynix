"""Process-title behavior for nanopynix managers and workers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import nanopynix._process_title as process_title
import nanopynix.rpc.client.session as nix_module
from nanopynix.rpc.client._pool import WorkerClient
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
    monkeypatch.setattr(process_title, "setproctitle", titles.append)
    monkeypatch.setattr(process_title, "generate_slug", lambda: "quiet-otter")

    returned = process_title.set_worker_title()

    assert titles == ["nanopynix (quiet-otter)"]
    assert returned == "quiet-otter", "the caller stores this as worker_subname"


def test_the_slug_is_two_words_from_the_two_lists() -> None:
    """The generator itself, which ``coolname`` used to provide.

    The test above passes a double, so it says nothing about the words. Issue
    #108 replaced the dependency with two lists in the module, and this is what
    reads them.
    """
    for _ in range(50):
        slug = process_title.generate_slug()
        adjective, separator, noun = slug.partition("-")

        assert separator == "-", f"{slug!r} is not two words joined by a hyphen"
        assert adjective in process_title._ADJECTIVES, f"{adjective!r} is not in the adjective list"
        assert noun in process_title._NOUNS, f"{noun!r} is not in the noun list"


def test_the_slug_lists_are_large_enough_to_tell_workers_apart() -> None:
    """A guard on the lists, not on the generator.

    The slug needs no uniqueness -- nothing reads it back, and the pid tells
    two workers apart. It does have to be worth reading, and a list that shrank
    to a handful of words would make every title look the same while every test
    above still passed.
    """
    combinations = len(process_title._ADJECTIVES) * len(process_title._NOUNS)

    assert combinations >= 1000, f"{combinations} slugs is too few to tell two workers apart in ps"
    assert len(set(process_title._ADJECTIVES)) == len(process_title._ADJECTIVES), "a duplicate adjective"
    assert len(set(process_title._NOUNS)) == len(process_title._NOUNS), "a duplicate noun"


def test_session_sets_manager_title(monkeypatch: pytest.MonkeyPatch) -> None:
    manager_titles: list[None] = []
    monkeypatch.setattr(nix_module, "set_manager_title", lambda: manager_titles.append(None))

    Session()

    assert manager_titles == [None]


@pytest.mark.anyio
async def test_store_session_opens_store_with_its_uri() -> None:
    pool = MagicMock(spec=WorkerClient)
    pool.worker_stub.open_store = AsyncMock(return_value=SimpleNamespace(store_handle=42))

    async def invoke(method: object, request: object, *, timeout: float) -> object:  # noqa: ASYNC109 -- mock implementing WorkerClient.invoke interface
        del timeout
        return await method(request)  # type: ignore[operator] -- test double receives a generated RPC method

    pool.invoke = invoke
    store = StoreHandle(pool, "local?root=/tmp/test-store", "session-id")

    await store.open()

    request = pool.worker_stub.open_store.call_args.args[0]
    assert request.uri == "local?root=/tmp/test-store"
