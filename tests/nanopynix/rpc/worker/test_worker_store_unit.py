"""Unit tests for concurrent L3 Store dispatch."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, cast

import anyio
import pytest
from nanopynix_proto.nix.store import GetUriRequest

from nanopynix.models import HandleKind
from nanopynix.rpc.worker._worker import WorkerState
from nanopynix.rpc.worker._worker_store import StoreServiceHandler


@pytest.mark.concurrency
async def test_store_handler_runs_independent_calls_on_multiple_store_threads() -> None:
    """The L3 Store handler must not serialize unrelated Store calls."""
    state = WorkerState()
    handles = state.handles
    barrier = threading.Barrier(2, timeout=2)
    active_lock = threading.Lock()
    active = 0
    peak_active = 0

    class SlowStore:
        def store_get_uri(self, _request: dict[str, Any]) -> dict[str, str]:
            nonlocal active, peak_active
            with active_lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                barrier.wait()
                return {"uri": "fake://store"}
            finally:
                with active_lock:
                    active -= 1

    handle = handles.allocate(SlowStore(), HandleKind.STORE)
    state.store_limiter = anyio.CapacityLimiter(2)
    handler = StoreServiceHandler(state)
    responses = cast(
        "tuple[Any, Any]",
        await asyncio.gather(
            handler.get_uri(GetUriRequest(store_handle=handle, request_id=1)),
            handler.get_uri(GetUriRequest(store_handle=handle, request_id=2)),
        ),
    )

    first, second = responses
    assert first.uri == "fake://store"
    assert second.uri == "fake://store"
    assert peak_active == 2
