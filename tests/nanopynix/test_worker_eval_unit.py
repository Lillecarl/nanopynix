"""Unit tests for worker eval store-handle selection."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import nanopynix._nix_core as nix_core  # type: ignore[reportPrivateUsage] -- test patches direct-pointer core boundary
import nanopynix._worker as worker  # type: ignore[reportPrivateUsage] -- test imports private module
from nanopynix._handle_registry import HandleRegistry  # type: ignore[reportPrivateUsage] -- test imports private module
from nanopynix._local import (  # type: ignore[reportPrivateUsage] -- test constructs worker's local runtime
    LocalEvalState,
    LocalRuntime,
    LocalStore,
)
from nanopynix._worker import (  # type: ignore[reportPrivateUsage] -- test imports private module
    WorkerServiceHandler,
    WorkerState,
)
from nanopynix._worker_eval import EvalServiceHandler  # type: ignore[reportPrivateUsage] -- test imports private module
from nanopynix._worker_nix import (
    NixThreadExecutor,  # type: ignore[reportPrivateUsage] -- test verifies thread confinement
)


class _FakeEvalState:
    def __init__(self, store: object, nix_path: list[str]) -> None:
        self.store = store
        self.nix_path = nix_path


class _FakeStore:
    def get_uri(self) -> str:
        return "local"

    def get_store_dir(self) -> str:
        return "/nix/store"


def test_worker_opens_auto_store_with_explicit_auto_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    opened_uris: list[str] = []

    def _open_store(uri: str) -> _FakeStore:
        opened_uris.append(uri)
        return _FakeStore()

    monkeypatch.setattr(nix_core.nanopynix_store, "open_store", _open_store)
    handler = WorkerServiceHandler(WorkerState())

    _handle, uri, store_dir = handler._open_store("auto")  # type: ignore[reportPrivateUsage] -- test verifies worker store dispatch

    assert opened_uris == ["auto"]
    assert uri == "local"
    assert store_dir == "/nix/store"


def test_worker_factory_sets_worker_title(monkeypatch: pytest.MonkeyPatch) -> None:
    titled: list[None] = []
    monkeypatch.setattr(worker, "set_worker_title", lambda: titled.append(None) or "quiet-otter")
    monkeypatch.setattr(worker.nanopynix_util, "install_logger", lambda _callback: None)  # type: ignore[reportUnknownLambdaType] -- lambda receives Any from setattr

    worker.worker_service_factory()

    assert titled == [None]


def test_worker_title_lists_open_store_uris(monkeypatch: pytest.MonkeyPatch) -> None:
    titles: list[str] = []
    monkeypatch.setattr(worker, "set_process_title", lambda title, **_kwargs: titles.append(title))  # type: ignore[reportUnknownLambdaType, reportUnknownArgumentType] -- lambda receives Any from setattr

    class Store:
        def __init__(self, uri: str) -> None:
            self.uri = uri

        def get_uri(self) -> str:
            return self.uri

        def get_store_dir(self) -> str:
            return "/nix/store"

    monkeypatch.setattr(nix_core.nanopynix_store, "open_store", lambda uri: Store(uri))  # type: ignore[reportUnknownLambdaType, reportUnknownArgumentType] -- lambda receives Any from setattr
    state = WorkerState()
    state.worker_subname = "quiet-otter"
    handler = WorkerServiceHandler(state)

    first, _uri, _store_dir = handler._open_store("local")  # type: ignore[reportPrivateUsage] -- test verifies worker store tracking
    second, _uri, _store_dir = handler._open_store("daemon")  # type: ignore[reportPrivateUsage] -- test verifies worker store tracking
    handler._close_store(first)  # type: ignore[reportPrivateUsage] -- test verifies worker store tracking
    handler._close_store(second)  # type: ignore[reportPrivateUsage] -- test verifies worker store tracking

    assert titles == ["local", "local daemon", "daemon", "quiet-otter"]


@pytest.mark.anyio
async def test_worker_initializes_nix_on_dedicated_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    initialized_on: list[int] = []

    def record(*_args: object, **_kwargs: object) -> None:
        initialized_on.append(threading.get_ident())

    monkeypatch.setattr(worker.nanopynix_util, "set_setting", record)
    monkeypatch.setattr(worker.nanopynix_util, "enable_experimental_feature", record)
    monkeypatch.setattr(worker.nanopynix_util, "init_libstore", record)
    monkeypatch.setattr(worker.nanopynix_util, "set_verbosity", record)
    monkeypatch.setattr(nix_core.nanopynix_expr, "init_libexpr", record)
    monkeypatch.setattr(nix_core.nanopynix_expr, "_set_pure_eval", record)
    monkeypatch.setattr(nix_core.nanopynix_expr, "_set_restrict_eval", record)
    monkeypatch.setattr(nix_core.nanopynix_expr, "_set_allowed_uris", record)
    monkeypatch.setattr(worker, "_register_primops", record)

    state = WorkerState()
    state.executor = NixThreadExecutor()
    state.rpc_bridge = object()  # type: ignore[assignment] -- only presence matters to the initialization path
    handler = WorkerServiceHandler(state)
    message = SimpleNamespace(
        settings={"sandbox": "false"},
        nix_conf=None,
        experimental_features=["flakes"],
        load_config=False,
        verbosity=None,
        pure_eval=True,
        restrict_eval=True,
        allowed_uris=["https://example.test"],
        nix_path=["nixpkgs=/tmp/nixpkgs"],
        primops=[],
    )

    try:
        response = await handler.init(message)
    finally:
        state.executor.shutdown()

    assert response.status == "ok"
    assert initialized_on
    assert all(thread_id != threading.get_ident() for thread_id in initialized_on)
    assert state.nix_path == ["nixpkgs=/tmp/nixpkgs"]


def test_open_eval_binds_eval_state_to_requested_store_handle(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(nix_core.nanopynix_expr, "EvalState", _FakeEvalState)

    handles = HandleRegistry()
    second_handle = handles.allocate(LocalStore("second-store"), "store")
    first_handle = handles.allocate(LocalStore("first-store"), "store")
    state = SimpleNamespace(
        eval_state=None,
        eval_store_handle=None,
        handles=handles,
        runtime=LocalRuntime(),
        nix_path=["nixpkgs=/tmp/nixpkgs"],
    )
    handler = EvalServiceHandler(state)

    handler._open_eval(second_handle)  # type: ignore[reportPrivateUsage] -- test verifies worker's Nix-thread lifecycle helper
    selected = handler._get_es()  # type: ignore[reportPrivateUsage] -- test accesses private method on handler

    assert isinstance(selected, LocalEvalState)
    assert isinstance(selected.raw, _FakeEvalState)
    assert selected.raw.store == "second-store"
    assert selected.raw.nix_path == ["nixpkgs=/tmp/nixpkgs"]
    assert state.eval_store_handle == second_handle

    assert handler._get_es() is selected  # type: ignore[reportPrivateUsage] -- test accesses private method on handler

    with pytest.raises(RuntimeError, match="already open"):
        handler._open_eval(first_handle)  # type: ignore[reportPrivateUsage] -- test verifies one-EvalState invariant

    assert state.eval_state is selected
    assert state.eval_store_handle == second_handle


def test_releasing_remote_value_closes_local_value() -> None:
    class Value:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    handles = HandleRegistry()
    value = Value()
    handle = handles.allocate(value, "value")
    handler = EvalServiceHandler(SimpleNamespace(handles=handles))

    handler._do_release(SimpleNamespace(handle=handle))  # type: ignore[reportPrivateUsage] -- test verifies worker release ownership

    assert value.closed
    with pytest.raises(KeyError):
        handles.get_typed(handle, "value")


def test_releasing_locked_flake_closes_local_resource() -> None:
    class LockedFlake:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    handles = HandleRegistry()
    locked_flake = LockedFlake()
    handle = handles.allocate(locked_flake, "locked_flake")
    handler = EvalServiceHandler(SimpleNamespace(handles=handles))

    handler._do_release_locked_flake(SimpleNamespace(handle=handle))  # type: ignore[reportPrivateUsage] -- test verifies worker release ownership

    assert locked_flake.closed
    with pytest.raises(KeyError):
        handles.get_typed(handle, "locked_flake")
