"""Unit tests for EvalSession + ValueProxy lifecycle using mocks.

No Nix daemon needed — exercises error paths and edge cases.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanopynix._session import EvalSession, ValueProxy

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


# ════════════════════════════════════════════════════════════════════
# ValueProxy lifecycle
# ════════════════════════════════════════════════════════════════════

class TestValueProxyLifecycle:
    def _worker(self):
        w = MagicMock()
        w.send_recv = AsyncMock()
        return w

    def test_handle_and_type_are_cached(self):
        w = self._worker()
        vp = ValueProxy(w, 42, "int")
        assert vp.handle == 42
        assert vp.type_name == "int"

    async def test_force_delegates_to_worker(self):
        w = self._worker()
        w.send_recv.return_value = 99
        vp = ValueProxy(w, 1, "int")
        result = await vp.force()
        assert result == 99
        w.send_recv.assert_awaited_with("eval", "force", [1], timeout=None)

    async def test_attr_returns_new_proxy(self):
        w = self._worker()
        w.send_recv.return_value = {"handle": 5, "type": "string"}
        vp = ValueProxy(w, 1, "attrs")
        child = await vp.attr("name")
        assert isinstance(child, ValueProxy)
        assert child.handle == 5
        assert child.type_name == "string"

    async def test_list_get_returns_new_proxy(self):
        w = self._worker()
        w.send_recv.return_value = {"handle": 3, "type": "int"}
        vp = ValueProxy(w, 1, "list")
        child = await vp.list_get(0)
        assert child.handle == 3

    async def test_list_length(self):
        w = self._worker()
        w.send_recv.return_value = 3
        vp = ValueProxy(w, 1, "list")
        assert await vp.list_length() == 3

    async def test_attr_names(self):
        w = self._worker()
        w.send_recv.return_value = ["a", "b", "c"]
        vp = ValueProxy(w, 1, "attrs")
        assert await vp.attr_names() == ["a", "b", "c"]

    async def test_has_attr(self):
        w = self._worker()
        w.send_recv.return_value = True
        vp = ValueProxy(w, 1, "attrs")
        assert await vp.has_attr("foo") is True

    async def test_release(self):
        w = self._worker()
        w.send_recv.return_value = None
        vp = ValueProxy(w, 1, "int")
        await vp.release()
        w.send_recv.assert_awaited_with("eval", "release", [1], timeout=None)

    async def test_raises_after_session_close(self):
        w = self._worker()
        active = [True]
        vp = ValueProxy(w, 1, "int", _active=active)

        # Active — works
        w.send_recv.return_value = 42
        assert await vp.force() == 42

        # Session closed
        active[0] = False
        with pytest.raises(RuntimeError, match="EvalSession has been closed"):
            await vp.force()

    async def test_check_active_only_when_flag_provided(self):
        """_active=None means never expires (backwards compat)."""
        w = self._worker()
        w.send_recv.return_value = 42
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
# ReservedWorker
# ════════════════════════════════════════════════════════════════════

class TestReservedWorker:
    async def test_release_idempotent(self):
        from nanopynix._pool import ReservedWorker, _WorkerManager

        manager = MagicMock(spec=_WorkerManager)
        manager._release = MagicMock()
        worker = MagicMock()

        rw = ReservedWorker(manager, worker)
        await rw.release()
        manager._release.assert_called_once()

        # Second release should be a no-op
        await rw.release()
        manager._release.assert_called_once()  # still only once

    async def test_send_recv_after_release_raises(self):
        from nanopynix._pool import ReservedWorker, _WorkerManager

        manager = MagicMock(spec=_WorkerManager)
        worker = MagicMock()

        rw = ReservedWorker(manager, worker)
        await rw.release()

        with pytest.raises(RuntimeError, match="has been released"):
            await rw.send_recv("store", "get_uri", [])
