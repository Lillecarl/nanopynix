"""Unit tests for worker eval store-handle selection."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from nanopynix_proto.nix.eval import OpenEvalRequest

import nanopynix._core._nix_core as nix_core  # type: ignore[reportPrivateUsage] -- test patches direct-pointer core boundary
import nanopynix.rpc.worker._worker as worker  # type: ignore[reportPrivateUsage] -- test imports private module
from nanopynix._core._local import (  # type: ignore[reportPrivateUsage] -- test constructs worker's local runtime
    LocalEvalState,
    LocalStore,
)
from nanopynix._wire import HandleKind
from nanopynix.rpc.worker._handle_registry import (
    HandleRegistry,  # type: ignore[reportPrivateUsage] -- test imports private module
)
from nanopynix.rpc.worker._worker import (  # type: ignore[reportPrivateUsage] -- test imports private module
    WorkerServiceHandler,
    WorkerState,
)
from nanopynix.rpc.worker._worker_eval import (  # type: ignore[reportPrivateUsage] -- test imports private module
    EvalServiceHandler,
    close_eval_state,
)
from nanopynix.rpc.worker._worker_nix import (
    NixThreadExecutor,  # type: ignore[reportPrivateUsage] -- test verifies thread confinement
)


class _FakeBridge:
    """Only presence and start()/stop() matter to the initialization path."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _FakeEvalState:
    def __init__(
        self,
        store: object,
        nix_path: list[str],
        build_store: object | None = None,
        eval_settings: dict[str, str] | None = None,
        fetch_settings: dict[str, str] | None = None,
    ) -> None:
        self.store = store
        self.nix_path = nix_path
        self.build_store = build_store
        self.eval_settings = eval_settings
        self.fetch_settings = fetch_settings


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
    closed: list[str] = []
    monkeypatch.setattr(worker, "set_process_title", lambda title, **_kwargs: titles.append(title))  # type: ignore[reportUnknownLambdaType, reportUnknownArgumentType] -- lambda receives Any from setattr

    class Store:
        def __init__(self, uri: str) -> None:
            self.uri = uri

        def get_uri(self) -> str:
            return self.uri

        def get_store_dir(self) -> str:
            return "/nix/store"

        def close(self) -> None:
            closed.append(self.uri)

    monkeypatch.setattr(nix_core.nanopynix_store, "open_store", Store)  # type: ignore[reportUnknownArgumentType] -- callable receives Any from setattr
    state = WorkerState()
    state.worker_subname = "quiet-otter"
    handler = WorkerServiceHandler(state)

    first, _uri, _store_dir = handler._open_store("local")  # type: ignore[reportPrivateUsage] -- test verifies worker store tracking
    second, _uri, _store_dir = handler._open_store("daemon")  # type: ignore[reportPrivateUsage] -- test verifies worker store tracking
    handler._close_store(first)  # type: ignore[reportPrivateUsage] -- test verifies worker store tracking
    handler._close_store(second)  # type: ignore[reportPrivateUsage] -- test verifies worker store tracking

    assert titles == ["local", "local daemon", "daemon", "quiet-otter"]
    assert closed == ["local", "daemon"]


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
    monkeypatch.setattr(worker, "_register_primops", record)

    state = WorkerState()
    state.executor = NixThreadExecutor()
    state.rpc_bridge = _FakeBridge()  # type: ignore[assignment] -- only presence and start()/stop() matter to the initialization path
    handler = WorkerServiceHandler(state)
    message = SimpleNamespace(
        settings={"sandbox": "false"},
        nix_conf=None,
        experimental_features=["flakes"],
        load_config=False,
        verbosity=None,
        nix_path=["nixpkgs=/tmp/nixpkgs"],
        primops=[],
        request_id=1,
    )

    try:
        response = await handler.init(message)
    finally:
        state.executor.shutdown()

    assert response.status == "ok"
    assert initialized_on
    assert all(thread_id != threading.get_ident() for thread_id in initialized_on)
    assert state.nix_path == ["nixpkgs=/tmp/nixpkgs"]


@pytest.mark.anyio
@pytest.mark.concurrency
async def test_open_eval_allows_concurrent_eval_states(monkeypatch: pytest.MonkeyPatch, init_expr: object) -> None:
    # `init_expr` forces real Boehm GC initialization: this test's open_eval
    # spins up real NixThreadExecutor instances with the real
    # _enter_evaluator_thread/_exit_evaluator_thread GC registration hooks,
    # which require nix::initGC() to have already run once in this process.
    del init_expr
    monkeypatch.setattr(nix_core.nanopynix_expr, "EvalState", _FakeEvalState)

    state = WorkerState()
    second_handle = state.handles.allocate(LocalStore("second-store"), HandleKind.STORE)
    first_handle = state.handles.allocate(LocalStore("first-store"), HandleKind.STORE)
    state.nix_path = ["nixpkgs=/tmp/nixpkgs"]
    handler = EvalServiceHandler(state)

    response = await handler.open_eval(OpenEvalRequest(store_handle=second_handle, request_id=1))
    selected = handler._get_es(response.eval_handle)  # type: ignore[reportPrivateUsage] -- test accesses private method on handler

    assert isinstance(selected, LocalEvalState)
    assert isinstance(selected.raw, _FakeEvalState)
    assert selected.raw.store == "second-store"
    assert selected.raw.nix_path == ["nixpkgs=/tmp/nixpkgs"]

    assert handler._get_es(response.eval_handle) is selected  # type: ignore[reportPrivateUsage] -- test accesses private method on handler

    # A second, concurrently open EvalState is now allowed rather than rejected.
    second_response = await handler.open_eval(OpenEvalRequest(store_handle=first_handle, request_id=2))
    assert second_response.eval_handle != response.eval_handle
    second_selected = handler._get_es(second_response.eval_handle)  # type: ignore[reportPrivateUsage] -- test accesses private method on handler
    assert second_selected.raw.store == "first-store"
    assert second_selected is not selected

    close_eval_state(state, response.eval_handle)
    close_eval_state(state, second_response.eval_handle)


def test_releasing_remote_value_closes_local_value() -> None:
    class Value:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    handles = HandleRegistry()
    value = Value()
    handle = handles.allocate(value, HandleKind.VALUE)
    handler = EvalServiceHandler(SimpleNamespace(handles=handles))

    handler._do_release(SimpleNamespace(handle=handle))  # type: ignore[reportPrivateUsage] -- test verifies worker release ownership

    assert value.closed
    with pytest.raises(KeyError):
        handles.get_typed(handle, HandleKind.VALUE)


def test_releasing_locked_flake_closes_local_resource() -> None:
    class LockedFlake:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    handles = HandleRegistry()
    locked_flake = LockedFlake()
    handle = handles.allocate(locked_flake, HandleKind.LOCKED_FLAKE)
    handler = EvalServiceHandler(SimpleNamespace(handles=handles))

    handler._do_release_locked_flake(SimpleNamespace(handle=handle))  # type: ignore[reportPrivateUsage] -- test verifies worker release ownership

    assert locked_flake.closed
    with pytest.raises(KeyError):
        handles.get_typed(handle, HandleKind.LOCKED_FLAKE)
