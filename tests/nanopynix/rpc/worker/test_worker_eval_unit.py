"""Unit tests for worker eval store-handle selection."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import anyio
import nanopynix_bindings.store as nanopynix_store
import pytest
from nanopynix_proto.nix.eval import OpenEvalRequest
from nanopynix_proto.nix.worker import InitRequest

import nanopynix._core._nix_core as nix_core  # type: ignore[reportPrivateUsage] -- test patches direct-pointer core boundary
import nanopynix.rpc.worker._worker as worker  # type: ignore[reportPrivateUsage] -- test imports private module
from nanopynix._core._objects import (  # type: ignore[reportPrivateUsage] -- test constructs worker's local runtime
    CoreEvalState,
    CoreLockedFlake,
    CoreStore,
    CoreValue,
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
    EvalEntry,
    EvalServiceHandler,
    close_eval_state,
)
from nanopynix.rpc.worker._worker_nix import (
    NixThreadExecutor,  # type: ignore[reportPrivateUsage] -- test verifies thread confinement
)


class _UnusedEvalState(CoreEvalState):
    """A CoreEvalState with no native pointer, for a test that never touches it.

    ``EvalEntry.eval_state`` names the real type, so beartype checks it at run
    time and ``None`` no longer passes. Subclassing the real class is what the
    rest of this file already does for a double -- see ``_FakeEvalState``.
    """

    def __init__(self) -> None:
        self.raw = None


class _InlineDispatchState(WorkerState):
    """A ``WorkerState`` that runs each operation inline, on the calling task.

    A subclass rather than a stand-in object. ``EvalServiceHandler`` names
    ``WorkerState``, and beartype checks that at run time, so a duck-typed
    namespace no longer reaches the constructor.
    """

    def __init__(self, handles: HandleRegistry) -> None:
        super().__init__()
        self.handles = handles

    async def run_request(  # noqa: PLR0913 -- the signature it overrides, which the type checker requires it to match in full
        self,
        *,
        request_id: int,
        operation: Callable[..., Any],
        args: tuple[Any, ...] = (),
        executor: NixThreadExecutor | None = None,
        limiter: anyio.CapacityLimiter | None = None,
        verbosity: int | None = None,
    ) -> Any:
        _ = (request_id, executor, limiter, verbosity)
        return operation(*args)


def _handler_with_inline_dispatch(handles: HandleRegistry) -> tuple[EvalServiceHandler, int]:
    """A handler whose RPCs run their body inline, plus an evaluator handle.

    The handlers are ``@worker_op``-decorated sync bodies, so an entry point is
    the only way in -- there is no separately addressable ``_do_`` method any
    more. These two tests are about handle ownership rather than dispatch, so
    the state runs each operation inline, and the evaluator handle exists only
    because ``_run`` looks up an executor before dispatching.
    """
    # NixThreadExecutor spawns its worker thread lazily on first submitted
    # task, so constructing one here without ever running work on it is safe.
    eval_handle = handles.allocate(
        EvalEntry(eval_state=_UnusedEvalState(), executor=NixThreadExecutor(), store_handle=0),
        HandleKind.EVAL,
    )
    return EvalServiceHandler(_InlineDispatchState(handles)), eval_handle


class _FakeBridge:
    """Only presence and start()/stop() matter to the initialization path."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _FakeEvalState(nix_core.nanopynix_expr.EvalState):
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


def _fake_raw(eval_state: CoreEvalState) -> _FakeEvalState:
    """The double behind a ``CoreEvalState``, narrowed for the assertions below.

    ``_get_es`` was annotated ``Any``, which is what let these assertions reach
    ``.store`` on a binding class that has no such attribute. #16 typed it, so
    the narrowing has to be written down -- and asserting that the double is
    really in place says more than a cast does.
    """
    raw = eval_state.raw
    assert isinstance(raw, _FakeEvalState)
    return raw


class _StoreId(nanopynix_store.Store):
    """A store stand-in that is only ever compared by an identity string.

    ``_FakeEvalState`` never calls anything on the store it is given -- it
    just stashes it and the assertions below check *which* one open_eval
    picked -- so equality against the identity string is all that matters.
    """

    def __init__(self, ident: str) -> None:
        self._ident = ident

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and other == self._ident

    def __hash__(self) -> int:
        return hash(self._ident)

    def __repr__(self) -> str:
        return self._ident


class _FakeStore(nanopynix_store.Store):
    def __init__(self) -> None:
        pass

    # with_params mirrors the real binding, which declares it keyword-capable
    # ("with_params"_a): CoreStore.get_uri forwards it by name, so a fake that
    # takes no argument no longer stands in for one.
    def get_uri(self, with_params: bool = False) -> str:  # positional-or-keyword, as the binding declares it
        _ = with_params
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


async def test_worker_factory_sets_worker_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """Async because the factory starts the log relay, which needs a loop.

    That is a real precondition and not a test artefact: every caller of the
    factory invokes it from inside ``asyncio.run``. See ``_start_log_relay``.
    """
    titled: list[None] = []
    monkeypatch.setattr(worker, "set_worker_title", lambda: titled.append(None) or "quiet-otter")
    monkeypatch.setattr(worker.nanopynix_util, "install_logger", lambda _callback: None)  # type: ignore[reportUnknownLambdaType] -- lambda receives Any from setattr

    handlers = worker.worker_service_factory()
    try:
        assert titled == [None]
    finally:
        # Nothing serves these handlers, so nothing else will stop the relay.
        state = worker._worker_state_of(handlers)  # type: ignore[reportPrivateUsage] -- test cleans up what the factory started
        if state.log_task is not None:
            state.log_task.cancel()
            with contextlib.suppress(anyio.get_cancelled_exc_class()):
                await state.log_task


def test_worker_title_lists_open_store_uris(monkeypatch: pytest.MonkeyPatch) -> None:
    titles: list[str] = []
    closed: list[str] = []
    monkeypatch.setattr(worker, "set_process_title", lambda title, **_kwargs: titles.append(title))  # type: ignore[reportUnknownLambdaType, reportUnknownArgumentType] -- lambda receives Any from setattr

    class Store(nanopynix_store.Store):
        def __init__(self, uri: str) -> None:
            self.uri = uri

        def get_uri(self, with_params: bool = False) -> str:  # positional-or-keyword, as the binding declares it
            _ = with_params
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
    message = InitRequest(
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
    # These strings stand in for a store only as identity markers -- the
    # assertions below check *which* one open_eval picked, and _FakeEvalState
    # never calls anything on them. Cast because CoreStore now names the real
    # binding type rather than Any.
    second_handle = state.handles.allocate(CoreStore(_StoreId("second-store")), HandleKind.STORE)
    first_handle = state.handles.allocate(CoreStore(_StoreId("first-store")), HandleKind.STORE)
    state.nix_path = ["nixpkgs=/tmp/nixpkgs"]
    handler = EvalServiceHandler(state)

    response = await handler.open_eval(OpenEvalRequest(store_handle=second_handle, request_id=1))
    selected = handler._get_es(response.eval_handle)  # type: ignore[reportPrivateUsage] -- test accesses private method on handler

    assert isinstance(selected, CoreEvalState)
    assert isinstance(selected.raw, _FakeEvalState)
    assert selected.raw.store == "second-store"
    assert selected.raw.nix_path == ["nixpkgs=/tmp/nixpkgs"]

    assert handler._get_es(response.eval_handle) is selected  # type: ignore[reportPrivateUsage] -- test accesses private method on handler

    # A second, concurrently open EvalState is now allowed rather than rejected.
    second_response = await handler.open_eval(OpenEvalRequest(store_handle=first_handle, request_id=2))
    assert second_response.eval_handle != response.eval_handle
    second_selected = handler._get_es(second_response.eval_handle)  # type: ignore[reportPrivateUsage] -- test accesses private method on handler
    assert _fake_raw(second_selected).store == "first-store"
    assert second_selected is not selected

    close_eval_state(state, response.eval_handle)
    close_eval_state(state, second_response.eval_handle)


@pytest.mark.anyio
async def test_open_eval_forwards_the_build_store_handle(
    monkeypatch: pytest.MonkeyPatch,
    init_expr: object,
) -> None:
    """A build_store_handle must reach ``open_eval_state``'s third parameter.

    This is the plumbing witness for ``OpenEvalRequest.build_store_handle``.
    The cross-engine test in ``test_engine_parity_semantics`` cannot be it:
    both stores there come from the same session and resolve to the same
    underlying Nix store, so evaluation succeeds whether or not the handle is
    forwarded. Measured -- with the forwarding reverted to the hardcoded
    ``None`` it replaced, that test still passes and this one fails.
    """
    del init_expr  # as above: real GC init before a real NixThreadExecutor
    monkeypatch.setattr(nix_core.nanopynix_expr, "EvalState", _FakeEvalState)

    state = WorkerState()
    eval_store = state.handles.allocate(CoreStore(_StoreId("eval-store")), HandleKind.STORE)
    build_store = state.handles.allocate(CoreStore(_StoreId("build-store")), HandleKind.STORE)
    handler = EvalServiceHandler(state)

    with_build = await handler.open_eval(
        OpenEvalRequest(store_handle=eval_store, build_store_handle=build_store, request_id=1),
    )
    selected = handler._get_es(with_build.eval_handle)  # type: ignore[reportPrivateUsage] -- test accesses private method on handler
    assert _fake_raw(selected).store == "eval-store"
    assert _fake_raw(selected).build_store == "build-store", "the build store did not reach the evaluator"

    # 0 is the wire's "none", as for every other optional handle.
    without_build = await handler.open_eval(OpenEvalRequest(store_handle=eval_store, request_id=2))
    assert _fake_raw(handler._get_es(without_build.eval_handle)).build_store is None  # type: ignore[reportPrivateUsage] -- test accesses private method on handler

    close_eval_state(state, with_build.eval_handle)
    close_eval_state(state, without_build.eval_handle)


async def test_releasing_remote_value_closes_local_value() -> None:
    class Value(CoreValue):
        """Records the close, and holds no native pointer.

        Subclasses the real class because the typed accessor names it, and
        beartype checks the returned object at run time.
        """

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    handles = HandleRegistry()
    value = Value()
    handle = handles.allocate(value, HandleKind.VALUE)
    handler, eval_handle = _handler_with_inline_dispatch(handles)

    await handler.release(cast("Any", SimpleNamespace(handle=handle, eval_handle=eval_handle, request_id=1)))

    assert value.closed
    with pytest.raises(KeyError):
        handles.get_value(handle)


async def test_releasing_locked_flake_closes_local_resource() -> None:
    class LockedFlake(CoreLockedFlake):
        """Records the close, and holds no native pointer.

        Subclasses the real class because the typed accessor names it, and
        beartype checks the returned object at run time.
        """

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    handles = HandleRegistry()
    locked_flake = LockedFlake()
    handle = handles.allocate(locked_flake, HandleKind.LOCKED_FLAKE)
    handler, eval_handle = _handler_with_inline_dispatch(handles)

    await handler.release_locked_flake(
        cast("Any", SimpleNamespace(handle=handle, eval_handle=eval_handle, request_id=1)),
    )

    assert locked_flake.closed
    with pytest.raises(KeyError):
        handles.get_locked_flake(handle)
