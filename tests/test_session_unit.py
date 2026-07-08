"""Unit tests for EvalSession + ValueProxy lifecycle using mocks.

No Nix daemon needed — exercises error paths and edge cases.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanopynix import NixError
from nanopynix._pool import _ActiveCall, _WorkerManager
from nanopynix._session import EvalSession, ValueProxy
from nanopynix.models import LogEvent

pytestmark = pytest.mark.asyncio


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════


def _mock_pool():
    """Return a mock WorkerPool that supports reserve()."""
    pool = MagicMock()
    pool.reserve = AsyncMock()
    pool.rpc_timeout = 300.0
    return pool


def _mock_reserved_worker():
    """Return a mock ReservedWorker that delegates send_recv."""
    rw = MagicMock()
    rw.send_recv = AsyncMock()
    rw.release = AsyncMock()
    return rw


# ════════════════════════════════════════════════════════════════════
# EvalSession lifecycle
# ════════════════════════════════════════════════════════════════════


class TestEvalSessionLifecycle:
    async def test_enter_reserves_worker(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        pool.reserve.return_value = rw

        session = EvalSession(pool)
        result = await session.__aenter__()
        assert result is session
        pool.reserve.assert_awaited_once()

    async def test_open_close_manual_lifecycle(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw.send_recv.return_value = None
        pool.reserve.return_value = rw

        session = EvalSession(pool)
        await session.open()
        await session.close()

        pool.reserve.assert_awaited_once()
        rw.release.assert_awaited_once()

    async def test_exit_releases_worker(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw.send_recv.return_value = None  # release_all returns None
        pool.reserve.return_value = rw

        session = EvalSession(pool)
        await session.__aenter__()
        await session.__aexit__(None, None, None)

        rw.send_recv.assert_awaited_with("eval", "release_all", [], timeout=None)
        rw.release.assert_awaited_once()

    async def test_exit_releases_worker_even_on_release_all_error(self):
        """Worker is always returned to pool even if release_all RPC fails."""
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw.send_recv.side_effect = TimeoutError("release_all timed out")
        pool.reserve.return_value = rw

        session = EvalSession(pool)
        await session.__aenter__()
        # Exception propagates, but worker must still be released
        with pytest.raises(TimeoutError, match="release_all timed out"):
            await session.__aexit__(None, None, None)

        rw.release.assert_awaited_once()

    async def test_eval_file_before_enter_raises(self):
        pool = _mock_pool()
        session = EvalSession(pool)
        with pytest.raises(RuntimeError, match="not entered"):
            await session.eval_file("/some/path.nix")

    async def test_eval_string_before_enter_raises(self):
        pool = _mock_pool()
        session = EvalSession(pool)
        with pytest.raises(RuntimeError, match="not entered"):
            await session.eval_string("42")

    async def test_eval_file_after_enter(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw.send_recv.return_value = {"handle": 1, "type": "attrs"}
        pool.reserve.return_value = rw

        session = EvalSession(pool)
        await session.__aenter__()
        root = await session.eval_file("/some/path.nix")
        assert isinstance(root, ValueProxy)
        assert root.handle == 1
        assert root.type_name == "attrs"

    async def test_eval_string_after_enter(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw.send_recv.return_value = {"handle": 2, "type": "int"}
        pool.reserve.return_value = rw

        session = EvalSession(pool)
        await session.__aenter__()
        root = await session.eval_string("42 + 1")
        assert root.type_name == "int"

    async def test_timeout_override(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw.send_recv.return_value = {"handle": 1, "type": "int"}
        pool.reserve.return_value = rw

        session = EvalSession(pool, timeout=10.0)
        await session.__aenter__()
        await session.eval_string("42", timeout=5.0)
        rw.send_recv.assert_awaited_with("eval", "eval_string", ["42", "<string>"], timeout=5.0)

    async def test_timeout_falls_back_to_session_default(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw.send_recv.return_value = {"handle": 1, "type": "int"}
        pool.reserve.return_value = rw

        session = EvalSession(pool, timeout=10.0)
        await session.__aenter__()
        await session.eval_string("42")  # no override
        rw.send_recv.assert_awaited_with("eval", "eval_string", ["42", "<string>"], timeout=10.0)


class TestWorkerManagerActiveCall:
    async def test_fatal_log_event_fails_active_call(self):
        manager = _WorkerManager()
        fut = asyncio.get_running_loop().create_future()
        manager._active_call = _ActiveCall(req_id=7, future=fut)

        manager._fail_active_call_from_event(
            {"request_id": 7, "action": "error", "args": [0, "attribute 'x' missing"]}
        )

        assert fut.done()
        with pytest.raises(NixError, match="attribute 'x' missing"):
            fut.result()

    async def test_non_error_log_event_does_not_fail_active_call(self):
        manager = _WorkerManager()
        fut = asyncio.get_running_loop().create_future()
        manager._active_call = _ActiveCall(req_id=7, future=fut)

        manager._fail_active_call_from_event(
            {"request_id": 7, "action": "warn", "args": ["still running"]}
        )

        assert not fut.done()


# ════════════════════════════════════════════════════════════════════
# ValueProxy lifecycle
# ════════════════════════════════════════════════════════════════════


class TestValueProxyLifecycle:
    def _worker(self):
        w = MagicMock()
        w._send_recv = AsyncMock()
        return w

    def test_handle_and_type_are_cached(self):
        w = self._worker()
        vp = ValueProxy(w, 42, "int")
        assert vp.handle == 42
        assert vp.type_name == "int"

    async def test_force_delegates_to_worker(self):
        w = self._worker()
        w._send_recv.return_value = 99
        vp = ValueProxy(w, 1, "int")
        result = await vp.force()
        assert result == 99
        w._send_recv.assert_awaited_with("eval", "force", [1], timeout=None)

    async def test_attr_returns_new_proxy(self):
        w = self._worker()
        vp = ValueProxy(w, 1, "attrs")
        child = vp.attr("name")
        assert isinstance(child, ValueProxy)
        assert child.type_name == "unknown"
        w._send_recv.assert_not_awaited()

        w._send_recv.side_effect = [
            {"handle": 5, "type": "string"},
            "hello",
        ]
        assert await child.force() == "hello"
        assert child.handle == 5
        assert child.type_name == "string"

    async def test_list_get_returns_new_proxy(self):
        w = self._worker()
        vp = ValueProxy(w, 1, "list")
        child = vp.list_get(0)
        w._send_recv.assert_not_awaited()

        w._send_recv.side_effect = [
            {"handle": 3, "type": "int"},
            "int",
        ]
        assert await child.type() == "int"
        assert child.handle == 3

    async def test_list_length(self):
        w = self._worker()
        w._send_recv.return_value = 3
        vp = ValueProxy(w, 1, "list")
        assert await vp.list_length() == 3

    async def test_attr_names(self):
        w = self._worker()
        w._send_recv.return_value = ["a", "b", "c"]
        vp = ValueProxy(w, 1, "attrs")
        assert await vp.attr_names() == ["a", "b", "c"]

    async def test_has_attr(self):
        w = self._worker()
        w._send_recv.return_value = True
        vp = ValueProxy(w, 1, "attrs")
        assert await vp.has_attr("foo") is True

    async def test_type_delegates_to_worker(self):
        w = self._worker()
        w._send_recv.return_value = "attrs"
        vp = ValueProxy(w, 1, "thunk")
        assert await vp.type() == "attrs"
        assert vp.type_name == "attrs"
        w._send_recv.assert_awaited_with("eval", "type_name", [1], timeout=None)

    async def test_release(self):
        w = self._worker()
        w._send_recv.return_value = None
        vp = ValueProxy(w, 1, "int")
        await vp.release()
        w._send_recv.assert_awaited_with("eval", "release", [1], timeout=None)

    async def test_raises_after_session_close(self):
        w = self._worker()
        active = [True]
        vp = ValueProxy(w, 1, "int", _active=active)

        # Active — works
        w._send_recv.return_value = 42
        assert await vp.force() == 42

        # Session closed
        active[0] = False
        with pytest.raises(RuntimeError, match="EvalSession has been closed"):
            await vp.force()

    async def test_check_active_only_when_flag_provided(self):
        """_active=None means never expires (backwards compat)."""
        w = self._worker()
        w._send_recv.return_value = 42
        vp = ValueProxy(w, 1, "int")  # no _active
        assert await vp.force() == 42  # should not raise

    async def test_handle_still_accessible_after_close(self):
        """Cached properties are available even after session close."""
        w = self._worker()
        active = [True]
        vp = ValueProxy(w, 42, "attrs", _active=active)
        active[0] = False
        assert vp.handle == 42
        assert vp.type_name == "attrs"


# ════════════════════════════════════════════════════════════════════
# ValueProxy lazy child resolution
# ════════════════════════════════════════════════════════════════════


class TestLazyChildProxy:
    """Verify child proxies resolve via attr/list_get, not parent force."""

    def _worker(self):
        w = MagicMock()
        w._send_recv = AsyncMock()
        return w

    async def test_attrs_getitem_force_calls_attr(self):
        """attrs[\"x\"].force() calls eval.attr with parent handle and name."""
        w = self._worker()
        w._send_recv.side_effect = [
            {"handle": 5, "type": "int"},  # _resolve
            99,  # force
        ]
        cp = ValueProxy.child(w, 1, "name")

        result = await cp.force()

        assert w._send_recv.await_count == 2
        assert w._send_recv.await_args_list[0] == (("eval", "attr", [1, "name"]), {"timeout": None})
        assert w._send_recv.await_args_list[1] == (("eval", "force", [5]), {"timeout": None})
        assert result == 99

    async def test_list_getitem_force_calls_list_get(self):
        """lst[0].force() calls eval.list_get with parent handle and index."""
        w = self._worker()
        w._send_recv.side_effect = [
            {"handle": 3, "type": "int"},  # _resolve: list_get
            42,  # force: returns int
        ]
        cp = ValueProxy.child(w, 1, 0)

        result = await cp.force()

        assert w._send_recv.await_count == 2
        assert w._send_recv.await_args_list[0] == (("eval", "list_get", [1, 0]), {"timeout": None})
        assert w._send_recv.await_args_list[1] == (("eval", "force", [3]), {"timeout": None})
        assert result == 42

    async def test_no_rpc_until_force(self):
        """No RPC is made until .force() is called on the child proxy."""
        w = self._worker()
        cp = ValueProxy.child(w, 1, "name")
        w._send_recv.assert_not_called()
        # accessing a property doesn't trigger RPC either
        with pytest.raises(RuntimeError, match="not been resolved"):
            _ = cp.handle
        w._send_recv.assert_not_called()

    async def test_child_proxy_force_deep(self):
        """force_deep resolves child then deep-forces it."""
        w = self._worker()
        w._send_recv.side_effect = [
            {"handle": 5, "type": "attrs"},  # _resolve
            {"a": 1, "b": 2},  # force_deep
        ]
        cp = ValueProxy.child(w, 1, "name")

        result = await cp.force_deep()

        assert w._send_recv.await_count == 2
        assert w._send_recv.await_args_list[0] == (("eval", "attr", [1, "name"]), {"timeout": None})
        assert w._send_recv.await_args_list[1] == (("eval", "force_deep", [5]), {"timeout": None})
        assert result == {"a": 1, "b": 2}

    async def test_child_proxy_inactive_raises(self):
        """Child proxy raises after session close."""
        w = self._worker()
        active = [False]
        cp = ValueProxy.child(w, 1, "name", _active=active)

        with pytest.raises(RuntimeError, match="EvalSession has been closed"):
            await cp.force()

    async def test_child_proxy_timeout_override(self):
        """Timeout override is passed through to resolve and force."""
        w = self._worker()
        w._send_recv.side_effect = [
            {"handle": 5, "type": "int"},
            42,
        ]
        cp = ValueProxy.child(w, 1, "name", timeout=30.0)

        await cp.force(timeout=10.0)

        assert w._send_recv.await_args_list[0] == (("eval", "attr", [1, "name"]), {"timeout": 10.0})


# ════════════════════════════════════════════════════════════════════
# ReservedWorker
# ════════════════════════════════════════════════════════════════════


class TestReservedWorker:
    async def test_release_idempotent(self):
        from nanopynix._pool import ReservedWorker, _WorkerManager

        manager = MagicMock(spec=_WorkerManager)
        manager._release = MagicMock()

        rw = ReservedWorker(manager)
        await rw.release()
        manager._release.assert_called_once()

        # Second release should be a no-op
        await rw.release()
        manager._release.assert_called_once()  # still only once

    async def test_send_recv_after_release_raises(self):
        from nanopynix._pool import ReservedWorker, _WorkerManager

        manager = MagicMock(spec=_WorkerManager)

        rw = ReservedWorker(manager)
        await rw.release()

        with pytest.raises(RuntimeError, match="has been released"):
            await rw.send_recv("store", "get_uri", [])


# ════════════════════════════════════════════════════════════════════
# log_stream request-id handling (C1 fix)
# ════════════════════════════════════════════════════════════════════


class TestLogStreamRequestId:
    """Verify Session.log_stream() correctly maps worker wire format to LogEvent."""

    @staticmethod
    def _events_to_log_stream(events: list):
        """Return an async generator that yields the given events."""

        async def _gen():
            for e in events:
                yield e

        return _gen()

    async def test_worker_request_id_mapped_correctly(self):
        """Worker emits ``request_id`` — log_stream produces valid LogEvent."""
        from nanopynix.nix import Session

        session = Session.__new__(Session)
        manager = MagicMock()
        manager.log_stream = MagicMock(
            return_value=self._events_to_log_stream(
                [
                    {"request_id": 42, "action": "msg", "args": [3, "hello from nix"]},
                    {"request_id": 7, "action": "start", "args": [0, "building"]},
                    {"request_id": 7, "action": "result", "args": [0, 100]},
                ]
            )
        )
        session._manager = manager

        events = [e async for e in session.log_stream()]

        assert len(events) == 3
        for e in events:
            assert isinstance(e, LogEvent)

        assert events[0].request_id == 42
        assert events[0].action == "msg"
        assert events[0].args == [3, "hello from nix"]
        assert events[0].result_type is None

        assert events[1].request_id == 7
        assert events[1].action == "start"

        assert events[2].request_id == 7
        assert events[2].action == "result"
        assert events[2].result_type == 100

    async def test_legacy_id_key_fallback(self):
        """Legacy ``id`` key is still accepted as fallback."""
        from nanopynix.nix import Session

        session = Session.__new__(Session)
        manager = MagicMock()
        manager.log_stream = MagicMock(
            return_value=self._events_to_log_stream(
                [
                    {"id": 99, "action": "msg", "args": [3, "legacy"]},
                ]
            )
        )
        session._manager = manager

        events = [e async for e in session.log_stream()]
        assert len(events) == 1
        assert events[0].request_id == 99
        assert events[0].action == "msg"

    async def test_missing_both_keys_defaults_zero(self):
        """Missing both keys defaults to request_id=0."""
        from nanopynix.nix import Session

        session = Session.__new__(Session)
        manager = MagicMock()
        manager.log_stream = MagicMock(
            return_value=self._events_to_log_stream(
                [
                    {"action": "msg", "args": [3, "no id"]},
                ]
            )
        )
        session._manager = manager

        events = [e async for e in session.log_stream()]
        assert len(events) == 1
        assert events[0].request_id == 0

    async def test_none_sentinel_skipped(self):
        """None sentinel from log_stream is skipped."""
        from nanopynix.nix import Session

        session = Session.__new__(Session)
        manager = MagicMock()
        manager.log_stream = MagicMock(
            return_value=self._events_to_log_stream(
                [
                    None,
                    {"request_id": 1, "action": "msg", "args": [3, "after sentinel"]},
                ]
            )
        )
        session._manager = manager

        events = [e async for e in session.log_stream()]
        assert len(events) == 1
        assert events[0].request_id == 1


class TestLogCapture:
    async def test_capture_records_typed_events(self):
        from nanopynix._pool import _WorkerManager
        from nanopynix.nix import Session

        session = Session.__new__(Session)
        manager = _WorkerManager()
        session._manager = manager

        async with session.capture_logs() as logs:
            manager._log_bus.emit({"request_id": 4, "action": "msg", "args": [3, "hello"]})
            manager._log_bus.emit({"request_id": 4, "action": "result", "args": [1, 100]})

        assert [event.action for event in logs.events] == ["msg", "result"]
        assert logs.events[0].request_id == 4
        assert logs.events[0].args == [3, "hello"]
        assert logs.events[1].result_type == 100

    async def test_capture_unsubscribes_on_exit(self):
        from nanopynix._pool import _WorkerManager
        from nanopynix.nix import Session

        session = Session.__new__(Session)
        manager = _WorkerManager()
        session._manager = manager

        async with session.capture_logs() as logs:
            manager._log_bus.emit({"request_id": 1, "action": "msg", "args": ["inside"]})

        manager._log_bus.emit({"request_id": 1, "action": "msg", "args": ["outside"]})

        assert len(logs.events) == 1
        assert logs.events[0].args == ["inside"]
