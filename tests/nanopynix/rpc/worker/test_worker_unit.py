"""Unit tests for WorkerServiceHandler lifecycle methods and small worker helpers.

Complements test_worker_eval_unit.py (store/eval dispatch) and
test_worker_store_unit.py (Store concurrency): this file targets the
lifecycle RPCs (init/open_store/close_store/verbosity/subscribe_logs/
shutdown) error-guard branches, plus _import_callable/_register_primops/
_install_worker_diagnostics, none of which had direct unit coverage.
"""

from __future__ import annotations

import contextlib
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from grpclib.exceptions import GRPCError
from nanopynix_proto.nix.common import LogLevel
from nanopynix_proto.nix.worker import (
    CloseStoreRequest,
    GetVerbosityRequest,
    InitRequest,
    OpenStoreRequest,
    SetVerbosityRequest,
    ShutdownRequest,
    SubscribeLogsRequest,
)

import nanopynix.rpc.worker._worker as worker  # type: ignore[reportPrivateUsage] -- test imports private module
from nanopynix._wire import HandleKind
from nanopynix.logging import LogCollector, LogOutbox
from nanopynix.rpc.worker._worker import (  # type: ignore[reportPrivateUsage] -- test imports private module
    WorkerServiceHandler,
    WorkerState,
    _import_callable,
    _install_worker_diagnostics,
    _register_primops,
    _relay_logs,
    _start_log_relay,
)
from nanopynix.rpc.worker._worker_nix import (
    NixThreadExecutor,  # type: ignore[reportPrivateUsage] -- test verifies thread confinement
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator


class _FakeBridge:
    """Only presence and start()/stop() matter to the initialization/shutdown path."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def test_import_callable_rejects_a_path_without_a_colon() -> None:
    with pytest.raises(ValueError, match="invalid primop import path"):
        _import_callable("nanopynix.rpc.worker._worker")


def test_import_callable_rejects_a_non_callable_target() -> None:
    with pytest.raises(TypeError, match="primop import path is not callable"):
        _import_callable("nanopynix.rpc.worker._worker:DEFAULT_WORKER_MAX_CONCURRENCY")


def test_import_callable_resolves_a_dotted_attribute_path() -> None:
    resolved = _import_callable("nanopynix.rpc.worker._worker:WorkerState.__init__")

    assert resolved is WorkerState.__init__


def test_register_primops_requires_a_bridge_for_rpc_primops() -> None:
    with pytest.raises(RuntimeError, match="registered without backchannel"):
        _register_primops(
            [{"name": "rpcOp", "arity": 1, "args": ["x"], "doc": "", "import_path": "", "rpc": True}],
            rpc_bridge=None,
        )


def test_install_worker_diagnostics_dumps_stats_on_signal(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # capfd (not capsys): faulthandler.dump_traceback() needs a real fileno(),
    # which capsys's in-memory replacement stream does not provide.
    installed: list[Any] = []

    def _fake_signal(_sig: int, handler: Any) -> None:
        installed.append(handler)

    monkeypatch.setattr(worker.signal, "signal", _fake_signal)

    _install_worker_diagnostics(LogCollector(), LogOutbox())

    assert len(installed) == 1
    installed[0](0, None)  # simulate the OS delivering SIGUSR1

    captured = capfd.readouterr()
    assert "nanopynix worker diagnostics" in captured.err
    assert "log_collector=" in captured.err
    # Both halves of the log path. A deep collector and a deep outbox mean
    # different things, and the dump is useless if it shows only one.
    assert "log_outbox=" in captured.err
    assert "end nanopynix worker diagnostics" in captured.err


async def test_run_request_rejects_a_non_positive_request_id() -> None:
    state = WorkerState()
    finalized: list[int] = []
    state.collector = SimpleNamespace(request_finalized=finalized.append)  # type: ignore[assignment] -- narrow collector fake
    executor = NixThreadExecutor()
    try:
        with pytest.raises(ValueError, match="request_id must be positive"):
            await state.run_request(request_id=0, executor=executor, operation=lambda: None)
    finally:
        executor.shutdown()
    assert finalized == [0]


def test_log_without_a_collector_is_a_no_op() -> None:
    state = WorkerState()

    state.log("some-action", 1, 2)  # must not raise even though collector is None


async def test_init_requires_an_executor() -> None:
    handler = WorkerServiceHandler(WorkerState())
    message = InitRequest(settings={}, nix_conf=None, request_id=1)

    with pytest.raises(GRPCError, match="worker executor is unavailable"):
        await handler.init(message)


async def test_init_writes_nix_conf_and_skips_empty_settings_render(monkeypatch: pytest.MonkeyPatch) -> None:
    def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(worker.nanopynix_util, "set_setting", _noop)
    monkeypatch.setattr(worker.nanopynix_util, "enable_experimental_feature", _noop)
    monkeypatch.setattr(worker.nanopynix_util, "init_libstore", _noop)
    monkeypatch.setattr(worker.nanopynix_util, "set_verbosity", _noop)
    monkeypatch.setattr(worker, "_register_primops", _noop)
    monkeypatch.delenv("NIX_USER_CONF_FILES", raising=False)

    state = WorkerState()
    state.executor = NixThreadExecutor()
    state.rpc_bridge = _FakeBridge()  # type: ignore[assignment] -- only presence and start()/stop() matter to the initialization path
    handler = WorkerServiceHandler(state)
    message = InitRequest(
        settings={},
        nix_conf="/tmp/fake-nix.conf",
        experimental_features=[],
        load_config=False,
        verbosity=None,
        nix_path=[],
        primops=[],
        request_id=1,
    )

    try:
        response = await handler.init(message)
    finally:
        state.executor.shutdown()

    assert response.status == "ok"
    assert os.environ["NIX_USER_CONF_FILES"] == "/tmp/fake-nix.conf"


async def test_init_reports_and_reraises_initialization_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("nix init exploded")

    monkeypatch.setattr(worker.nanopynix_util, "enable_experimental_feature", _boom)

    state = WorkerState()
    state.executor = NixThreadExecutor()
    state.rpc_bridge = _FakeBridge()  # type: ignore[assignment] -- only presence and start()/stop() matter to the initialization path
    handler = WorkerServiceHandler(state)
    message = InitRequest(
        settings={},
        nix_conf=None,
        experimental_features=["flakes"],
        load_config=False,
        verbosity=None,
        nix_path=[],
        primops=[],
        request_id=1,
    )

    try:
        with pytest.raises(GRPCError, match="nix init exploded"):
            await handler.init(message)
    finally:
        state.executor.shutdown()

    assert "nix init exploded" in capsys.readouterr().err


async def test_open_store_requires_a_store_limiter() -> None:
    handler = WorkerServiceHandler(WorkerState())

    with pytest.raises(GRPCError, match="worker store limiter is unavailable"):
        await handler.open_store(OpenStoreRequest(request_id=1, uri="auto"))


async def test_close_store_requires_an_executor() -> None:
    handler = WorkerServiceHandler(WorkerState())

    with pytest.raises(GRPCError, match="worker executor is unavailable"):
        await handler.close_store(CloseStoreRequest(request_id=1, store_handle=1, force=False))


def test_close_store_skips_title_update_for_a_handle_never_opened_via_open_store() -> None:
    """A handle allocated by hand (bypassing _open_store) was never added to
    named_store_uris, so _close_store's title-refresh branch must be skipped."""
    titles: list[str] = []

    class _FakeStore:
        def close(self) -> None:
            pass

    def _record_title_update() -> None:
        titles.append("called")

    state = WorkerState()
    handle = state.handles.allocate(_FakeStore(), HandleKind.STORE)
    handler = WorkerServiceHandler(state)
    handler._update_store_title = _record_title_update  # type: ignore[method-assign, reportPrivateUsage] -- test verifies it is NOT called

    handler._close_store(handle)  # type: ignore[reportPrivateUsage] -- test verifies worker store bookkeeping

    assert titles == []


async def test_get_verbosity_requires_an_executor() -> None:
    handler = WorkerServiceHandler(WorkerState())

    with pytest.raises(GRPCError, match="worker executor is unavailable"):
        await handler.get_verbosity(GetVerbosityRequest(request_id=1))


async def test_set_verbosity_requires_an_executor() -> None:
    handler = WorkerServiceHandler(WorkerState())

    with pytest.raises(GRPCError, match="worker executor is unavailable"):
        await handler.set_verbosity(SetVerbosityRequest(request_id=1, verbosity=LogLevel.NOTICE))


@asynccontextmanager
async def _worker_with_log_relay() -> AsyncGenerator[tuple[WorkerState, Any]]:
    """A worker state whose collector is drained by the real relay task.

    ``subscribe_logs`` reads the outbox and only the relay fills it, so a test
    that feeds the collector needs the relay running. That is also how the
    worker runs: both runners call ``_start_log_relay`` right after the
    factory, which is what this calls too.
    """
    state = WorkerState()
    state.collector = LogCollector()
    state.outbox = LogOutbox()
    handler = WorkerServiceHandler(state)
    _start_log_relay(state)
    try:
        yield state, handler
    finally:
        task = state.log_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(anyio.get_cancelled_exc_class()):
                await task


async def test_subscribe_logs_returns_immediately_without_an_outbox() -> None:
    handler = WorkerServiceHandler(WorkerState())

    events = [event async for event in handler.subscribe_logs(SubscribeLogsRequest())]

    assert events == []


async def test_subscribe_logs_yields_nix_and_finalized_events() -> None:
    async with _worker_with_log_relay() as (state, handler):
        collector = state.collector
        assert collector is not None
        collector.callback(1, "msg", "hello")
        collector.request_finalized(1)
        collector.send_sentinel()

        with anyio.fail_after(5):
            events = [event async for event in handler.subscribe_logs(SubscribeLogsRequest())]

    assert len(events) == 2
    assert events[0].request_id == 1
    # `nix_log` is an optional proto field, so it gets the same presence check
    # the finalized branch below already had. It was missing here only because
    # `wrap_service_handlers` erased the handler's type and pyright could not
    # see the access at all.
    assert events[0].nix_log is not None
    assert events[0].nix_log.action == "msg"
    assert events[1].request_id == 1
    assert events[1].request_finalized is not None


async def test_the_relay_announces_what_the_collector_dropped() -> None:
    """A drop reaches the caller as an event, not only as a worker-side counter.

    Without this the client reads a short log and cannot tell that it is short.
    The notice comes before the event that follows the loss, so its position in
    the stream says which part is incomplete.
    """
    state = WorkerState()
    state.collector = LogCollector(maxsize=2)
    state.outbox = LogOutbox()
    handler = WorkerServiceHandler(state)

    for index in range(6):
        state.collector.callback(1, "msg", f"line {index}")
    assert state.collector.stats()["dropped"] == 4

    _start_log_relay(state)
    # The queue is still full, so the sentinel takes the place of one more log
    # line rather than being lost itself: a control event is never discarded.
    # That is the fifth drop the relay announces below.
    state.collector.send_sentinel()
    assert state.collector.stats()["dropped"] == 5

    with anyio.fail_after(5):
        events = [event async for event in handler.subscribe_logs(SubscribeLogsRequest())]

    dropped = [event for event in events if event.events_dropped is not None]
    assert len(dropped) == 1
    assert dropped[0].events_dropped is not None
    assert dropped[0].events_dropped.count == 5


async def test_the_relay_breaks_on_a_none_sentinel_from_the_stream() -> None:
    """collector.stream() itself never yields None (it breaks internally first);
    this defends the same guard directly against a differently-behaved collector.

    The guard moved from ``subscribe_logs`` to ``_relay_logs`` when the relay
    took over the decoding; without it the tuple unpack raises on the None.
    """

    class _SentinelCollector(LogCollector):
        """Yields the None that the real ``stream`` swallows.

        A subclass rather than a stand-in object, because ``_relay_logs``
        annotates the concrete class and beartype checks that annotation.
        """

        async def stream(self) -> AsyncIterator[Any]:
            yield None

    outbox = LogOutbox()
    with anyio.fail_after(5):
        await _relay_logs(_SentinelCollector(), outbox)

    # Only the end-of-stream sentinel the relay adds for subscribe_logs.
    assert await outbox.get() is None


async def test_shutdown_requires_an_executor() -> None:
    handler = WorkerServiceHandler(WorkerState())

    with pytest.raises(GRPCError, match="worker executor is unavailable"):
        await handler.shutdown(ShutdownRequest(request_id=1))


async def test_shutdown_ends_the_subscribe_logs_stream() -> None:
    """The worker closes its own log stream, so the client never has to cancel.

    Cancelling instead resets a stream whose handler is still inside its
    ``async for``, and grpclib logs that as a "Failed to handle cancellation"
    traceback on the worker's stderr -- which the parent process inherits.
    Asserting on that race directly would be flaky (it lost about 4 times in
    180 sessions); this pins the mechanism that removes it.
    """
    async with _worker_with_log_relay() as (state, handler):
        state.executor = NixThreadExecutor()

        await handler.shutdown(ShutdownRequest(request_id=1))

        # Terminates rather than hanging: shutdown left a sentinel behind for it.
        with anyio.fail_after(5):
            events = [event async for event in handler.subscribe_logs(SubscribeLogsRequest())]

    # The sentinel goes in *after* shutdown finalizes its own request, so the
    # stream still delivers that boundary before ending. Truncating it would
    # leave the client waiting on a finalization that never arrives.
    assert [event.request_id for event in events] == [1]
    assert events[0].request_finalized is not None


async def test_shutdown_tolerates_no_bridge_and_no_store_limiter() -> None:
    state = WorkerState()
    state.executor = NixThreadExecutor()
    state.rpc_bridge = None
    state.store_limiter = None
    handler = WorkerServiceHandler(state)

    response = await handler.shutdown(ShutdownRequest(request_id=1))

    assert response is not None
