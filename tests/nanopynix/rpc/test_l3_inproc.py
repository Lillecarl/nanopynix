"""L3 lifecycle tests over real in-process H2 with observable worker state.

Unlike the normal subprocess L3 tests, these retain the worker service
handlers in this process.  That lets each test assert both the public client
behaviour and the exact ``HandleRegistry`` state behind the RPC boundary.
"""

from __future__ import annotations

import asyncio
import gc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from grpclib.exceptions import GRPCError
from grpclib_transports import inproc_worker_with_backchannel
from nanopynix_bindings import util as nanopynix_util
from nanopynix_proto.nix.eval import EvalServiceStub, EvalStringRequest
from nanopynix_proto.nix.worker import (
    CloseStoreRequest,
    InitRequest,
    OpenStoreRequest,
    ShutdownRequest,
    WorkerServiceStub,
)

from nanopynix._core._nix_executor import NixThreadExecutor
from nanopynix.models import HandleKind, NixType
from nanopynix.rpc.client._manager import ManagerPrimopServiceHandler
from nanopynix.rpc.client._session import EvalSession, ValueReleasedError
from nanopynix.rpc.client.store import StoreHandle
from nanopynix.rpc.worker._worker import WorkerServiceHandler, WorkerState, worker_service_factory
from tests.support.git import init_flake_repo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from tests.support.nix_environment import NixTestEnvironment

pytestmark = pytest.mark.l3_inproc


@dataclass
class _InprocWorkerClient:
    """Minimal worker-client shape used by the public L3 EvalSession."""

    eval_stub: Any
    worker_stub: Any
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _next_request_id: int = 3

    async def invoke(self, method: Any, request: Any, *, timeout: float) -> Any:  # noqa: ASYNC109 -- mock implementing WorkerClient.invoke interface
        del timeout
        async with self._lock:
            request.request_id = self._next_request_id
            self._next_request_id += 1
            return await method(request)


class _InprocManager:
    def __init__(self, worker: _InprocWorkerClient) -> None:
        self._worker = worker
        self.worker_stub = worker.worker_stub
        self.reserve_count = 0

    async def reserve(self, *, timeout: float | None = None) -> _InprocWorkerClient:  # noqa: ASYNC109 -- threading timeout through to grpclib, not asyncio.timeout
        del timeout
        self.reserve_count += 1
        return self._worker

    async def invoke(self, method: Any, request: Any, *, timeout: float) -> Any:  # noqa: ASYNC109 -- timeout forwarded to InprocWorkerClient.invoke matching grpclib interface
        return await self._worker.invoke(method, request, timeout=timeout)


class _FailFirstRpc:
    """Forward one stub method after injecting a single transport failure."""

    def __init__(self, delegate: Any, method_name: str) -> None:
        self._delegate = delegate
        self._method_name = method_name
        self._failed = False

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._delegate, name)
        if name != self._method_name:
            return method

        async def fail_once(*args: Any, **kwargs: Any) -> Any:
            if not self._failed:
                self._failed = True
                raise OSError(f"injected {name} transport failure")
            return await method(*args, **kwargs)

        return fail_once


@dataclass
class _L3Inproc:
    eval: EvalSession
    manager: _InprocManager
    state: WorkerState
    worker: _InprocWorkerClient
    worker_stub: WorkerServiceStub
    initial_store_handle: int
    store_uri: str


@pytest.fixture
async def l3_inproc(isolated_nix_environment: NixTestEnvironment) -> AsyncIterator[_L3Inproc]:
    handlers: list[object] = []
    store_uri = isolated_nix_environment.store_uri
    executor = NixThreadExecutor()
    previous_verbosity = await executor.run(nanopynix_util.get_verbosity)

    def service_factory(backchannel: Any) -> list[Any]:
        result = worker_service_factory(backchannel, executor=executor)
        handlers.extend(result)
        return result

    async with inproc_worker_with_backchannel(service_factory, [ManagerPrimopServiceHandler()]) as channel:
        worker_stub = WorkerServiceStub(channel)
        eval_stub = EvalServiceStub(channel)
        await worker_stub.init(
            InitRequest(
                request_id=1,
                store_uri=store_uri,
                load_config=False,
                settings=isolated_nix_environment.settings.to_worker_settings(),
                experimental_features=["flakes"],
            ),
        )
        store = await worker_stub.open_store(OpenStoreRequest(request_id=2, uri=store_uri))
        worker = _InprocWorkerClient(eval_stub, worker_stub)
        manager = _InprocManager(worker)
        session = EvalSession(cast("Any", worker), store_handle=store.store_handle)
        worker_handler = cast("WorkerServiceHandler", handlers[0])  # type: ignore[reportUnknownVariableType] -- service decorator has no static type information
        state = worker_handler._state  # type: ignore[reportPrivateUsage, reportUnknownVariableType] -- test intentionally observes the decorated in-process worker
        worker_state = cast("WorkerState", state)
        try:
            yield _L3Inproc(  # type: ignore[reportUnknownArgumentType] -- decorated handler state is runtime-typed above
                session,
                manager,
                worker_state,
                worker,
                worker_stub,
                store.store_handle,
                store_uri,
            )
        finally:
            try:
                await session.close()
                await worker_stub.close_store(CloseStoreRequest(request_id=1_000_000, store_handle=store.store_handle))
                assert worker_state.handles.iter_kind(HandleKind.VALUE) == []
                assert worker_state.handles.iter_kind(HandleKind.LOCKED_FLAKE) == []
                assert worker_state.handles.iter_kind(HandleKind.STORE) == []
                await worker_stub.shutdown(ShutdownRequest(request_id=1_000_001))
                await executor.run(nanopynix_util.remove_logger)
                await executor.run(nanopynix_util.set_verbosity, previous_verbosity)
            finally:
                executor.shutdown(wait=True)


async def test_attrs_and_children_have_exactly_one_owner(l3_inproc: _L3Inproc) -> None:
    async with l3_inproc.eval as eval:
        parent = await eval.string("{ x = 1; }")
        assert [handle for handle, _ in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)] == [parent.handle]

        attrs = await parent.force_as(NixType.ATTRS)
        child = attrs["x"]
        assert [handle for handle, _ in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)] == [parent.handle]

        assert await child.force() == 1
        handles = {handle for handle, _ in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)}
        assert handles == {parent.handle, child.handle}

        await parent.release()
        assert {handle for handle, _ in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)} == {child.handle}
        await child.release()
        assert l3_inproc.state.handles.iter_kind(HandleKind.VALUE) == []


async def test_lists_and_children_have_exactly_one_owner(l3_inproc: _L3Inproc) -> None:
    async with l3_inproc.eval as eval:
        parent = await eval.string("[ 1 ]")
        values = await parent.force_as(NixType.LIST)
        child = values[0]
        assert {handle for handle, _ in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)} == {parent.handle}

        assert await child.force() == 1
        assert {handle for handle, _ in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)} == {
            parent.handle,
            child.handle,
        }

        await parent.release()
        assert {handle for handle, _ in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)} == {child.handle}
        await child.release()
        assert l3_inproc.state.handles.iter_kind(HandleKind.VALUE) == []


async def test_value_finalizer_defers_then_drains_to_worker(l3_inproc: _L3Inproc) -> None:
    async with l3_inproc.eval as eval:
        value = await eval.string("1")
        handle = value.handle
        del value
        gc.collect()

        assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)} == {handle}
        await eval.string("2")
        assert handle not in {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)}


async def test_borrowing_view_keeps_parent_alive_until_the_view_is_dropped(l3_inproc: _L3Inproc) -> None:
    async with l3_inproc.eval as eval:
        parent = await eval.string("{ x = 1; }")
        handle = parent.handle
        attrs = await parent.force_as(NixType.ATTRS)

        del parent
        gc.collect()
        assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)} == {handle}

        child = attrs["x"]
        assert await child.force() == 1
        assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)} == {handle, child.handle}

        await child.release()
        del child, attrs
        gc.collect()
        await eval.string("2")
        assert handle not in {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)}


async def test_borrowing_list_keeps_parent_alive_until_the_view_is_dropped(l3_inproc: _L3Inproc) -> None:
    async with l3_inproc.eval as eval:
        parent = await eval.string("[ 1 ]")
        handle = parent.handle
        values = await parent.force_as(NixType.LIST)

        del parent
        gc.collect()
        assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)} == {handle}

        child = values[0]
        assert await child.force() == 1
        await child.release()
        del child, values
        gc.collect()
        await eval.string("2")
        assert handle not in {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)}


async def test_explicit_value_release_is_idempotent_at_both_sides(l3_inproc: _L3Inproc) -> None:
    async with l3_inproc.eval as eval:
        value = await eval.string("1")
        handle = value.handle

        await value.release()
        await value.release()

        assert handle not in {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)}
        with pytest.raises(ValueReleasedError, match="ValueProxy"):
            await value.force()


async def test_failed_value_release_retries_at_the_next_rpc_boundary(l3_inproc: _L3Inproc) -> None:
    async with l3_inproc.eval as eval:
        value = await eval.string("1")
        handle = value.handle
        l3_inproc.worker.eval_stub = _FailFirstRpc(l3_inproc.worker.eval_stub, "release")

        with pytest.raises(OSError, match="injected release"):
            await value.release()
        assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)} == {handle}

        await eval.string("2")
        assert handle not in {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)}


async def test_session_close_closes_eval_state_and_clears_worker_values(l3_inproc: _L3Inproc) -> None:
    eval = l3_inproc.eval
    await eval.open()
    first = await eval.string("1")
    second = await eval.string("2")
    assert len(l3_inproc.state.handles.iter_kind(HandleKind.VALUE)) == 2

    await eval.close()

    assert l3_inproc.state.handles.iter_kind(HandleKind.VALUE) == []
    assert l3_inproc.state.handles.iter_kind(HandleKind.EVAL) == []
    assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.STORE)} == {l3_inproc.initial_store_handle}
    del first, second


async def test_eval_rpc_requires_open_eval(l3_inproc: _L3Inproc) -> None:
    with pytest.raises(GRPCError, match="call OpenEval before evaluating"):
        await l3_inproc.worker.eval_stub.eval_string(EvalStringRequest(expr="1", source_name="<test>"))

    assert l3_inproc.state.handles.iter_kind(HandleKind.EVAL) == []


async def test_store_cannot_close_while_its_eval_state_is_open(l3_inproc: _L3Inproc) -> None:
    await l3_inproc.eval.open()

    with pytest.raises(GRPCError) as error:
        await l3_inproc.worker_stub.close_store(
            CloseStoreRequest(request_id=1_000_002, store_handle=l3_inproc.initial_store_handle),
        )

    assert "call CloseEval first" in str(error.value)
    await l3_inproc.worker_stub.close_store(
        CloseStoreRequest(request_id=1_000_003, store_handle=l3_inproc.initial_store_handle, force=True),
    )
    assert l3_inproc.state.handles.iter_kind(HandleKind.EVAL) == []
    assert l3_inproc.state.handles.iter_kind(HandleKind.STORE) == []


async def test_locked_flake_explicit_release_removes_exact_worker_handle(
    l3_inproc: _L3Inproc,
    tmp_path: Path,
) -> None:
    init_flake_repo(tmp_path, "val = 1;")
    async with l3_inproc.eval as eval:
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
        handle = locked.handle
        assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.LOCKED_FLAKE)} == {handle}

        outputs = await locked.eval()
        output_handle = outputs.handle
        await locked.release()
        await locked.release()

        assert l3_inproc.state.handles.iter_kind(HandleKind.LOCKED_FLAKE) == []
        assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)} == {output_handle}
        assert await outputs.attr("val").force() == 1
        with pytest.raises(ValueReleasedError, match="LockedFlakeHandle"):
            await locked.eval()


async def test_failed_locked_flake_release_retries_at_the_next_rpc_boundary(
    l3_inproc: _L3Inproc,
    tmp_path: Path,
) -> None:
    init_flake_repo(tmp_path, "val = 1;")
    async with l3_inproc.eval as eval:
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
        handle = locked.handle
        l3_inproc.worker.eval_stub = _FailFirstRpc(l3_inproc.worker.eval_stub, "release_locked_flake")

        with pytest.raises(OSError, match="injected release_locked_flake"):
            await locked.release()
        assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.LOCKED_FLAKE)} == {handle}

        await eval.string("1")
        assert handle not in {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.LOCKED_FLAKE)}


async def test_locked_flake_finalizer_defers_then_drains_to_worker(l3_inproc: _L3Inproc, tmp_path: Path) -> None:
    init_flake_repo(tmp_path, "val = 1;")
    async with l3_inproc.eval as eval:
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
        handle = locked.handle
        del locked
        gc.collect()

        assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.LOCKED_FLAKE)} == {handle}
        await eval.string("1")
        assert handle not in {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.LOCKED_FLAKE)}


async def test_session_close_closes_eval_state_and_clears_values_and_locked_flakes(
    l3_inproc: _L3Inproc,
    tmp_path: Path,
) -> None:
    init_flake_repo(tmp_path, "val = 1;")
    eval = l3_inproc.eval
    await eval.open()
    value = await eval.string("1")
    locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
    assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)} == {value.handle}
    assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.LOCKED_FLAKE)} == {locked.handle}

    await eval.close()

    assert l3_inproc.state.handles.iter_kind(HandleKind.VALUE) == []
    assert l3_inproc.state.handles.iter_kind(HandleKind.LOCKED_FLAKE) == []
    assert l3_inproc.state.handles.iter_kind(HandleKind.EVAL) == []
    assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.STORE)} == {l3_inproc.initial_store_handle}
    del value, locked


async def test_late_finalizer_from_closed_generation_cannot_release_new_values(l3_inproc: _L3Inproc) -> None:
    eval = l3_inproc.eval
    await eval.open()
    stale = await eval.string("1")
    await eval.close()
    assert l3_inproc.state.handles.iter_kind(HandleKind.VALUE) == []

    await eval.open()
    live = await eval.string("2")
    live_handle = live.handle
    del stale
    gc.collect()
    await eval.string("3")

    assert live_handle in {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.VALUE)}
    await eval.close()


async def test_store_handles_are_distinct_and_close_exactly_once(l3_inproc: _L3Inproc) -> None:
    first = StoreHandle(cast("Any", l3_inproc.manager), l3_inproc.store_uri, "inproc")
    second = StoreHandle(cast("Any", l3_inproc.manager), l3_inproc.store_uri, "inproc")
    await first.open()
    await second.open()
    first_handle = first.store_handle
    second_handle = second.store_handle

    assert first_handle != second_handle
    assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.STORE)} == {
        l3_inproc.initial_store_handle,
        first_handle,
        second_handle,
    }

    await first.close()
    assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.STORE)} == {
        l3_inproc.initial_store_handle,
        second_handle,
    }
    await second.close()
    assert {item[0] for item in l3_inproc.state.handles.iter_kind(HandleKind.STORE)} == {l3_inproc.initial_store_handle}
