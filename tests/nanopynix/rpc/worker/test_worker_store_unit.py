"""Unit tests for concurrent L3 Store dispatch."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

from nanopynix_proto.nix.store import GetUriRequest

from nanopynix._core._nix_executor import NixThreadExecutor
from nanopynix.rpc.worker._handle_registry import HandleRegistry
from nanopynix.rpc.worker._worker_store import StoreServiceHandler


async def test_store_handler_runs_independent_calls_on_multiple_store_threads() -> None:
    """The L3 Store handler must not serialize unrelated Store calls."""
    handles = HandleRegistry()
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

    handle = handles.allocate(SlowStore(), "store")
    store_executor = NixThreadExecutor(max_workers=2, thread_name_prefix="test-store")
    handler = StoreServiceHandler(SimpleNamespace(handles=handles, store_executor=store_executor))
    try:
        first, second = await asyncio.gather(
            handler.get_uri(GetUriRequest(store_handle=handle)),
            handler.get_uri(GetUriRequest(store_handle=handle)),
        )
    finally:
        store_executor.shutdown(wait=True)

    assert first.uri == "fake://store"
    assert second.uri == "fake://store"
    assert peak_active == 2
