"""Unit tests for EvalSession + ValueProxy lifecycle using mocks.

No Nix daemon needed — exercises error paths and edge cases.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownLambdaType=false, reportUnknownArgumentType=false
# The entire file exercises pool/session internals via mock access.
# All members and variables accessed on MagicMock objects are inherently unknown.

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from nanopynix_proto.nix.common import LogEvent as LogEventProto

import nanopynix._pool as pool_module
from nanopynix import (
    BuildMode,
    EvalSessionClosedError,
    ForeignValueError,
    NixCoercionError,
    NixType,
    UnresolvedValueError,
    ValueReleasedError,
    WrongNixTypeError,
)
from nanopynix._pool import _RPC_TIMEOUT as _RPC_TIMEOUT
from nanopynix._pool import ReservedWorker, _WorkerManager as _WorkerManager
from nanopynix._session import (
    EvalProxy,
    EvalSession,
    ReplSession,
    ValueList,
    ValueProxy,
    _EvalOwner as _EvalOwner,
    _EvalOwnerToken as _EvalOwnerToken,
    _EvalProxyContext as _EvalProxyContext,
    _ResolvedValue as _ResolvedValue,
)
from nanopynix.models import LogEvent
from nanopynix.nix import Session
from nanopynix.store import StoreHandle

# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════


def _make_eval_stub() -> MagicMock:
    """Create a MagicMock that acts as an EvalServiceStub."""
    stub = MagicMock()
    stub.eval_file = AsyncMock()
    stub.eval_string = AsyncMock()
    stub.begin_repl = AsyncMock()
    stub.repl_process_line = AsyncMock()
    stub.force = AsyncMock()
    stub.force_deep = AsyncMock()
    stub.force_json = AsyncMock()
    stub.attr = AsyncMock()
    stub.list_get = AsyncMock()
    stub.list_length = AsyncMock()
    stub.attr_names = AsyncMock()
    stub.has_attr = AsyncMock()
    stub.type_name = AsyncMock()
    stub.build = AsyncMock()
    stub.call = AsyncMock()
    stub.lock_flake = AsyncMock()
    stub.call_locked_flake = AsyncMock()
    stub.write_lock_file = AsyncMock()
    stub.release_locked_flake = AsyncMock()
    stub.eval_flake = AsyncMock()
    stub.get_flake = AsyncMock()
    stub.release = AsyncMock()
    stub.release_all = AsyncMock()
    return stub


def _mock_value_handle(handle: int = 1, type_str: str = "int"):
    """Return a MagicMock that looks like a ValueHandle proto."""
    vh = MagicMock()
    vh.handle = handle
    vh.type = type_str
    return vh


def _mock_force_value_scalar(value: Any) -> MagicMock:
    """Return a ForceValue proto mock with scalar."""
    fv = MagicMock()
    scalar = MagicMock()
    scalar.string_value = value if isinstance(value, str) else None
    scalar.int_value = value if isinstance(value, int) and not isinstance(value, bool) else None
    scalar.float_value = value if isinstance(value, float) else None
    scalar.bool_value = value if isinstance(value, bool) else None
    scalar.null_value = MagicMock() if value is None else None
    fv.scalar = scalar
    fv.remote_value = None
    return fv


def _mock_build_response(
    *,
    drv_path: str = "/nix/store/aaa-demo.drv",
    output_path: str = "/nix/store/aaa-demo",
) -> SimpleNamespace:
    build_result = MagicMock()
    build_result.success = True
    build_result.error_msg = ""
    return SimpleNamespace(
        drv_path=drv_path,
        outputs={"out": output_path},
        results=[build_result],
    )


def _mock_force_value_remote(handle: int = 3, type_str: str = "int") -> MagicMock:  # type: ignore[reportUnusedFunction] -- kept for future test scenarios
    """Return a ForceValue proto mock with remote_value."""
    fv = MagicMock()
    fv.scalar = None
    fv.remote_value = _mock_value_handle(handle, type_str)
    return fv


def _mock_type_name_response(type_str: str = "attrs") -> MagicMock:
    resp = MagicMock()
    resp.type = type_str
    return resp


def _mock_list_length_response(length: int = 3) -> MagicMock:
    resp = MagicMock()
    resp.length = length
    return resp


def _mock_attr_names_response(names: list[str] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.names = names or ["a", "b", "c"]
    return resp


def _mock_has_attr_response(has: bool = True) -> MagicMock:
    resp = MagicMock()
    resp.has = has
    return resp


def _mock_pool():
    """Return a mock WorkerPool that supports reserve()."""
    pool = MagicMock()
    pool.reserve = AsyncMock()
    pool._eval_stub = _make_eval_stub()
    pool._store_stub = MagicMock()
    return pool


def _mock_reserved_worker():
    """Return a mock ReservedWorker with eval_stub."""
    rw = MagicMock()
    rw._eval_stub = _make_eval_stub()
    rw._store_stub = MagicMock()
    rw._store_stub.build_paths_with_results = AsyncMock()
    rw._store_stub.build_for_humans = AsyncMock()
    rw._store_stub.build_derivation = AsyncMock()
    rw._store_stub.read_derivation = AsyncMock()
    rw.release = AsyncMock()

    async def _call(coro: Any) -> Any:
        return await coro

    rw.call = _call
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
        pool.reserve.return_value = rw

        session = EvalSession(pool)
        await session.open()
        await session.close()

        pool.reserve.assert_awaited_once()
        rw.release.assert_awaited_once()

    async def test_exit_releases_worker(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw._eval_stub.release_all.return_value = MagicMock()
        pool.reserve.return_value = rw

        session = EvalSession(pool)
        await session.__aenter__()
        await session.__aexit__(None, None, None)

        rw._eval_stub.release_all.assert_awaited_once()
        rw.release.assert_awaited_once()

    async def test_exit_releases_worker_even_on_release_all_error(self):
        """Worker is always returned to pool even if release_all RPC fails."""
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw._eval_stub.release_all.side_effect = TimeoutError("release_all timed out")
        pool.reserve.return_value = rw

        session = EvalSession(pool)
        await session.__aenter__()
        # Exception propagates, but worker must still be released
        with pytest.raises(TimeoutError, match="release_all timed out"):
            await session.__aexit__(None, None, None)

        rw.release.assert_awaited_once()

    async def test_file_before_enter_raises(self):
        pool = _mock_pool()
        session = EvalSession(pool)
        with pytest.raises(EvalSessionClosedError, match="not entered"):
            await session.file("/some/path.nix")

    async def test_string_before_enter_raises(self):
        pool = _mock_pool()
        session = EvalSession(pool)
        with pytest.raises(EvalSessionClosedError, match="not entered"):
            await session.string("42")

    async def test_file_after_enter(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw._eval_stub.eval_file.return_value = _mock_value_handle(1, "attrs")
        pool.reserve.return_value = rw

        session = EvalSession(pool)
        await session.__aenter__()
        root = await session.file("/some/path.nix")
        assert isinstance(root, ValueProxy)
        assert root.handle == 1
        assert root.nix_type == NixType.ATTRS
        request = rw._eval_stub.eval_file.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert request.store_handle == 1

    async def test_string_after_enter(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw._eval_stub.eval_string.return_value = _mock_value_handle(2, "int")
        pool.reserve.return_value = rw

        session = EvalSession(pool, store_handle=99)
        await session.__aenter__()
        root = await session.string("42 + 1")
        assert root.nix_type == NixType.INT
        request = rw._eval_stub.eval_string.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert request.store_handle == 99

    async def test_timeout_override(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw._eval_stub.eval_string.return_value = _mock_value_handle(1, "int")
        pool.reserve.return_value = rw

        session = EvalSession(pool, timeout=10.0)
        await session.__aenter__()
        await session.string("42", timeout=5.0)
        call_kwargs = rw._eval_stub.eval_string.call_args[1]  # type: ignore[reportUnknownMemberType, reportOptionalSubscript] -- mock call_args absence in stubs
        assert call_kwargs["timeout"] == _RPC_TIMEOUT

    async def test_timeout_falls_back_to_session_default(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw._eval_stub.eval_string.return_value = _mock_value_handle(1, "int")
        pool.reserve.return_value = rw

        session = EvalSession(pool, timeout=10.0)
        await session.__aenter__()
        await session.string("42")  # no override
        call_kwargs = rw._eval_stub.eval_string.call_args[1]  # type: ignore[reportUnknownMemberType, reportOptionalSubscript] -- mock call_args absence in stubs
        assert call_kwargs["timeout"] == _RPC_TIMEOUT


class TestReplSession:
    async def test_open_starts_repl_scope_and_line_returns_expression_value(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw._eval_stub.repl_process_line.return_value = SimpleNamespace(
            is_binding=False,
            value=_mock_value_handle(7, "int"),
        )
        pool.reserve.return_value = rw

        session = ReplSession(pool, store_handle=99)
        await session.open()
        result = await session.line("x + 1")

        rw._eval_stub.begin_repl.assert_awaited_once()
        begin_request = rw._eval_stub.begin_repl.call_args.args[0]
        assert begin_request.store_handle == 99
        assert isinstance(result, ValueProxy)
        assert result.handle == 7

    async def test_line_returns_none_for_binding(self):
        pool = _mock_pool()
        rw = _mock_reserved_worker()
        rw._eval_stub.repl_process_line.return_value = SimpleNamespace(is_binding=True)
        pool.reserve.return_value = rw

        session = ReplSession(pool)
        await session.open()

        assert await session.line("x = 1") is None


class TestSessionEvalFacade:
    def _session(self) -> Session:
        session = object.__new__(Session)
        session._manager = _mock_pool()  # type: ignore[reportPrivateUsage] -- test injects mock manager
        session._session_id = "session-id"  # type: ignore[reportPrivateUsage] -- test injects internal session ID
        return session

    def _store(self, session_id: str = "session-id", handle: int = 42) -> StoreHandle:
        store = StoreHandle(_mock_pool(), "mock", session_id)
        store._active = True  # type: ignore[reportPrivateUsage] -- test injects internal store state
        store._store_handle = handle  # type: ignore[reportPrivateUsage] -- test injects store handle directly for mock setup
        return store

    def test_eval_uses_explicit_store_handle(self):
        session = self._session()
        store = self._store(handle=123)

        eval_session = session.eval(store)

        assert eval_session._store_handle == 123  # type: ignore[reportPrivateUsage] -- test inspects internal store handle

    def test_eval_rejects_foreign_store(self):
        session = self._session()
        store = self._store(session_id="other-session")

        with pytest.raises(ValueError, match="different session"):
            session.eval(store)

    def test_eval_rejects_closed_store(self):
        session = self._session()
        store = StoreHandle(_mock_pool(), "mock", "session-id")

        with pytest.raises(RuntimeError, match="StoreHandle is closed"):
            session.eval(store)


# ════════════════════════════════════════════════════════════════════
# ValueProxy lifecycle
# ════════════════════════════════════════════════════════════════════


class TestValueProxyLifecycle:
    def _worker(self) -> MagicMock:
        return _mock_reserved_worker()

    def _owner(self, active: list[bool] | None = None) -> _EvalOwner:
        return _EvalOwner(_EvalOwnerToken(), active)

    def _proxy(
        self,
        worker: MagicMock,
        handle: int,
        typ: NixType | str | None,
        *,
        owner: _EvalOwner | None = None,
        timeout: float | None = None,
    ) -> ValueProxy:
        return _EvalProxyContext(EvalProxy(worker), owner or self._owner(), timeout).value(handle, typ)

    async def test_handle_and_type_are_cached(self):
        w = self._worker()
        vp = self._proxy(w, 42, "int")
        assert vp.handle == 42
        assert vp.nix_type == NixType.INT

    async def test_force_delegates_to_worker(self):
        w = self._worker()
        w._eval_stub.force.return_value = _mock_force_value_scalar(99)
        vp = self._proxy(w, 1, "int")
        result = await vp.force()
        assert result == 99

    async def test_force_as_uses_cached_type(self):
        w = self._worker()
        w._eval_stub.force.return_value = _mock_force_value_scalar(99)
        vp = self._proxy(w, 1, "int")

        result = await vp.force_as(NixType.INT)

        assert result == 99
        w._eval_stub.force.assert_awaited_once()

    async def test_force_as_wrong_type_raises_typed_error(self):
        w = self._worker()
        vp = self._proxy(w, 1, "string")

        with pytest.raises(WrongNixTypeError) as exc:
            await vp.force_as(NixType.INT)

        assert exc.value.expected == "int"
        assert exc.value.actual == "string"
        w._eval_stub.force.assert_not_awaited()

    async def test_coerce_str_accepts_scalars(self):
        cases: list[tuple[NixType | str, object, str]] = [
            (NixType.STRING, "hello", "hello"),
            (NixType.PATH, "/nix/store/demo", "/nix/store/demo"),
            (NixType.INT, 42, "42"),
            (NixType.FLOAT, 1.5, "1.5"),
            (NixType.BOOL, True, "true"),
            (NixType.BOOL, False, "false"),
            (NixType.NULL, None, "null"),
        ]
        for typ, raw, expected in cases:
            w = self._worker()
            w._eval_stub.force.return_value = _mock_force_value_scalar(raw)
            vp = self._proxy(w, 1, typ)
            assert await vp.coerce_str() == expected

    async def test_coerce_int_accepts_integral_values(self):
        cases: list[tuple[NixType | str, object, int]] = [
            (NixType.INT, 42, 42),
            (NixType.FLOAT, 42.0, 42),
            (NixType.STRING, "  +42 ", 42),
            (NixType.STRING, "-7", -7),
        ]
        for typ, raw, expected in cases:
            w = self._worker()
            w._eval_stub.force.return_value = _mock_force_value_scalar(raw)
            vp = self._proxy(w, 1, typ)
            assert await vp.coerce_int() == expected

    async def test_coerce_int_rejects_ambiguous_values(self):
        cases: list[tuple[NixType | str, object]] = [
            (NixType.FLOAT, 1.5),
            (NixType.STRING, "1.5"),
            (NixType.STRING, "abc"),
            (NixType.BOOL, True),
        ]
        for typ, raw in cases:
            w = self._worker()
            w._eval_stub.force.return_value = _mock_force_value_scalar(raw)
            vp = self._proxy(w, 1, typ)
            with pytest.raises(NixCoercionError):
                await vp.coerce_int()

    async def test_coerce_float_accepts_finite_numbers(self):
        cases: list[tuple[NixType | str, object, float]] = [
            (NixType.INT, 42, 42.0),
            (NixType.FLOAT, 1.5, 1.5),
            (NixType.STRING, "  2.25 ", 2.25),
        ]
        for typ, raw, expected in cases:
            w = self._worker()
            w._eval_stub.force.return_value = _mock_force_value_scalar(raw)
            vp = self._proxy(w, 1, typ)
            assert await vp.coerce_float() == expected

    async def test_coerce_float_rejects_non_finite_or_bool(self):
        cases: list[tuple[NixType | str, object]] = [
            (NixType.STRING, "nan"),
            (NixType.STRING, "inf"),
            (NixType.BOOL, False),
        ]
        for typ, raw in cases:
            w = self._worker()
            w._eval_stub.force.return_value = _mock_force_value_scalar(raw)
            vp = self._proxy(w, 1, typ)
            with pytest.raises(NixCoercionError):
                await vp.coerce_float()

    async def test_coerce_bool_is_conservative(self):
        true_worker = self._worker()
        true_worker._eval_stub.force.return_value = _mock_force_value_scalar("true")
        false_worker = self._worker()
        false_worker._eval_stub.force.return_value = _mock_force_value_scalar(" false ")
        bool_worker = self._worker()
        bool_worker._eval_stub.force.return_value = _mock_force_value_scalar(True)

        assert await self._proxy(true_worker, 1, "string").coerce_bool() is True
        assert await self._proxy(false_worker, 1, "string").coerce_bool() is False
        assert await self._proxy(bool_worker, 1, "bool").coerce_bool() is True

        int_worker = self._worker()
        int_worker._eval_stub.force.return_value = _mock_force_value_scalar(1)
        with pytest.raises(NixCoercionError):
            await self._proxy(int_worker, 1, "int").coerce_bool()

    async def test_coercions_reject_structural_values(self):
        w = self._worker()
        # force() on attrs returns ValueAttrs, not a scalar
        w._eval_stub.force.return_value = _mock_force_value_scalar(["x"])
        vp = self._proxy(w, 1, "attrs")

        for coercion in (vp.coerce_str, vp.coerce_int, vp.coerce_float, vp.coerce_bool):
            with pytest.raises(NixCoercionError):
                await coercion()

    async def test_call_json_arg_uses_explicit_wire_arg(self):
        w = self._worker()
        w._eval_stub.call.return_value = _mock_value_handle(3, "int")
        vp = self._proxy(w, 1, "function")

        result = await vp({"name": "demo"})

        assert result.handle == 3

    async def test_call_unknown_type_resolves_type_before_call(self):
        w = self._worker()
        w._eval_stub.type_name.return_value = _mock_type_name_response("function")
        w._eval_stub.call.return_value = _mock_value_handle(3, "int")
        vp = self._proxy(w, 1, "unknown")

        result = await vp({"name": "demo"})

        assert result.handle == 3
        w._eval_stub.type_name.assert_awaited_once()

    async def test_call_non_function_raises_typed_error(self):
        w = self._worker()
        vp = self._proxy(w, 1, "int")

        with pytest.raises(WrongNixTypeError) as exc:
            await vp()

        assert exc.value.expected == "function"
        assert exc.value.actual == "int"
        w._eval_stub.call.assert_not_awaited()

    async def test_call_value_proxy_arg_uses_remote_handle(self):
        w = self._worker()
        w._eval_stub.call.return_value = _mock_value_handle(3, "int")
        owner = self._owner()
        fn = self._proxy(w, 1, "function", owner=owner)
        arg = self._proxy(w, 2, "attrs", owner=owner)

        result = await fn(arg)

        assert result.handle == 3
        w._eval_stub.call.assert_awaited_once()

    async def test_call_nested_value_proxy_arg_uses_remote_handle(self):
        w = self._worker()
        w._eval_stub.call.return_value = _mock_value_handle(3, "int")
        owner = self._owner()
        fn = self._proxy(w, 1, "function", owner=owner)
        arg = self._proxy(w, 2, "attrs", owner=owner)

        result = await fn({"items": [arg, 1]})

        assert result.handle == 3
        w._eval_stub.call.assert_awaited_once()

    async def test_call_foreign_value_proxy_raises_typed_error(self):
        w = self._worker()
        fn = self._proxy(w, 1, "function", owner=self._owner())
        arg = self._proxy(w, 2, "attrs", owner=self._owner())

        with pytest.raises(ForeignValueError, match="another EvalSession"):
            await fn(arg)

        w._eval_stub.call.assert_not_awaited()

    async def test_call_nested_foreign_value_proxy_raises_typed_error(self):
        w = self._worker()
        fn = self._proxy(w, 1, "function", owner=self._owner())
        arg = self._proxy(w, 2, "attrs", owner=self._owner())

        with pytest.raises(ForeignValueError, match="another EvalSession"):
            await fn({"arg": [arg]})

        w._eval_stub.call.assert_not_awaited()

    async def test_attr_returns_new_proxy(self):
        w = self._worker()
        vp = self._proxy(w, 1, "attrs")
        child = vp.attr("name")
        assert isinstance(child, ValueProxy)
        assert child.nix_type == NixType.UNSPECIFIED
        w._eval_stub.attr.assert_not_awaited()

        w._eval_stub.attr.return_value = _mock_value_handle(5, "string")
        w._eval_stub.force.return_value = _mock_force_value_scalar("hello")
        assert await child.force() == "hello"
        assert child.handle == 5
        assert child.nix_type == NixType.STRING

    async def test_list_get_returns_new_proxy(self):
        w = self._worker()
        vp = self._proxy(w, 1, "list")
        child = vp.list_get(0)
        w._eval_stub.list_get.assert_not_awaited()

        w._eval_stub.list_get.return_value = _mock_value_handle(3, "int")
        w._eval_stub.type_name.return_value = _mock_type_name_response("int")
        assert await child.get_type() == NixType.INT
        assert child.handle == 3

    async def test_list_get_accepts_negative_index(self):
        w = self._worker()
        vp = self._proxy(w, 1, "list")

        # Negative indices are deferred to the worker (normalised against list_length).
        child = vp.list_get(-1)
        assert child is not None
        # The stub should not be called yet — list_get is lazy.
        w._eval_stub.list_get.assert_not_awaited()

    async def test_list_length(self):
        w = self._worker()
        w._eval_stub.list_length.return_value = _mock_list_length_response(3)
        vp = self._proxy(w, 1, "list")
        assert await vp.list_length() == 3

    async def test_attr_names(self):
        w = self._worker()
        w._eval_stub.attr_names.return_value = _mock_attr_names_response(["a", "b", "c"])
        vp = self._proxy(w, 1, "attrs")
        assert await vp.attr_names() == ["a", "b", "c"]

    async def test_has_attr(self):
        w = self._worker()
        w._eval_stub.has_attr.return_value = _mock_has_attr_response(True)
        vp = self._proxy(w, 1, "attrs")
        assert await vp.has_attr("foo") is True

    async def test_build_uses_cascading_build_by_default(self):
        w = self._worker()
        w._eval_stub.build.return_value = _mock_build_response()
        vp = _EvalProxyContext(EvalProxy(w), self._owner(), store_handle=123).value(1, "attrs")

        result = await vp.build()

        assert result == {"out": "/nix/store/aaa-demo"}
        w._eval_stub.build.assert_awaited_once()
        build_request = w._eval_stub.build.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert build_request.handle == 1
        assert build_request.build_mode == BuildMode.Normal.value
        assert build_request.build_store_handle == 0
        w._eval_stub.attr.assert_not_awaited()
        w._eval_stub.force_json.assert_not_awaited()
        w._store_stub.build_for_humans.assert_not_awaited()
        w._store_stub.build_paths_with_results.assert_not_awaited()
        w._store_stub.build_derivation.assert_not_awaited()
        w._store_stub.read_derivation.assert_not_awaited()

    async def test_build_mode_uses_cascading_build(self):
        w = self._worker()
        w._eval_stub.build.return_value = _mock_build_response()
        vp = _EvalProxyContext(EvalProxy(w), self._owner(), store_handle=123).value(1, "attrs")

        result = await vp.build(build_mode=BuildMode.Check)

        assert result == {"out": "/nix/store/aaa-demo"}
        w._eval_stub.build.assert_awaited_once()
        build_request = w._eval_stub.build.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert build_request.handle == 1
        assert build_request.build_mode == BuildMode.Check.value
        assert build_request.build_store_handle == 0
        w._store_stub.build_for_humans.assert_not_awaited()
        w._store_stub.build_paths_with_results.assert_not_awaited()
        w._store_stub.build_derivation.assert_not_awaited()

    async def test_build_store_overrides_build_store_not_eval_store(self):
        w = self._worker()
        w._eval_stub.build.return_value = _mock_build_response()
        build_store = StoreHandle(_mock_pool(), "mock", "session-id")
        build_store._active = True  # type: ignore[reportPrivateUsage] -- test injects internal store state
        build_store._store_handle = 456  # type: ignore[reportPrivateUsage] -- test injects store handle directly for mock setup
        ctx = _EvalProxyContext(EvalProxy(w), self._owner(), store_handle=123, session_id="session-id")
        vp = ctx.value(1, "attrs")

        result = await vp.build(store=build_store)

        assert result == {"out": "/nix/store/aaa-demo"}
        build_request = w._eval_stub.build.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert build_request.handle == 1
        assert build_request.build_store_handle == 456
        w._store_stub.build_for_humans.assert_not_awaited()

    async def test_build_rejects_foreign_build_store(self):
        w = self._worker()
        build_store = StoreHandle(_mock_pool(), "mock", "other-session")
        build_store._active = True  # type: ignore[reportPrivateUsage] -- test injects internal store state
        build_store._store_handle = 456  # type: ignore[reportPrivateUsage] -- test injects store handle directly for mock setup
        ctx = _EvalProxyContext(EvalProxy(w), self._owner(), store_handle=123, session_id="session-id")
        vp = ctx.value(1, "attrs")

        with pytest.raises(ValueError, match="different session"):
            await vp.build(store=build_store)

    async def test_get_type_delegates_to_worker_for_thunk(self):
        w = self._worker()
        w._eval_stub.type_name.return_value = _mock_type_name_response("attrs")
        vp = self._proxy(w, 1, "thunk")
        assert await vp.get_type() == NixType.ATTRS
        assert vp.nix_type == NixType.ATTRS
        w._eval_stub.type_name.assert_awaited_once()

    async def test_get_type_uses_cached_concrete_type(self):
        w = self._worker()
        vp = self._proxy(w, 1, "attrs")

        assert await vp.get_type() == NixType.ATTRS
        w._eval_stub.type_name.assert_not_awaited()

    async def test_release(self):
        w = self._worker()
        w._eval_stub.release.return_value = MagicMock()
        vp = self._proxy(w, 1, "int")
        await vp.release()
        w._eval_stub.release.assert_awaited_once()

    async def test_raises_after_session_close(self):
        w = self._worker()
        active = [True]
        vp = self._proxy(w, 1, "int", owner=self._owner(active))

        # Active — works
        w._eval_stub.force.return_value = _mock_force_value_scalar(42)
        assert await vp.force() == 42

        # Session closed
        active[0] = False
        with pytest.raises(EvalSessionClosedError, match="EvalSession has been closed"):
            await vp.force()

    async def test_release_then_force_raises_typed_error(self):
        w = self._worker()
        w._eval_stub.release.return_value = MagicMock()
        vp = self._proxy(w, 1, "int")
        await vp.release()

        with pytest.raises(ValueReleasedError, match="has been released"):
            await vp.force()

    async def test_check_active_only_when_owner_has_flag(self):
        """An owner without an active flag never expires."""
        w = self._worker()
        w._eval_stub.force.return_value = _mock_force_value_scalar(42)
        vp = self._proxy(w, 1, "int")
        assert await vp.force() == 42  # should not raise

    async def test_handle_still_accessible_after_close(self):
        """Cached properties are available even after session close."""
        w = self._worker()
        active = [True]
        vp = self._proxy(w, 42, "attrs", owner=self._owner(active))
        active[0] = False
        assert vp.handle == 42
        assert vp.nix_type == NixType.ATTRS


class TestValueListBounds:
    def _worker(self) -> MagicMock:
        return _mock_reserved_worker()

    def _list(self, worker: MagicMock, length: int = 2) -> ValueList:
        ctx = _EvalProxyContext(EvalProxy(worker), _EvalOwner(_EvalOwnerToken()), None)
        return ValueList(ctx, _ResolvedValue(1, NixType.LIST), length)

    async def test_getitem_rejects_out_of_range_indexes(self):
        w = self._worker()
        value = self._list(w, length=2)

        # In-range: negative index normalised, positive index in range — both ok.
        assert value[-1] is not None
        assert value[0] is not None

        # Out of range: positive index past end, negative index too far back.
        with pytest.raises(IndexError, match="out of range"):
            _ = value[2]
        with pytest.raises(IndexError, match="out of range"):
            _ = value[-3]

        w._eval_stub.list_get.assert_not_awaited()

    async def test_force_rejects_out_of_range_indexes(self):
        w = self._worker()
        value = self._list(w, length=2)

        with pytest.raises(IndexError, match="out of range"):
            await value.force(2)

        w._eval_stub.list_get.assert_not_awaited()


# ════════════════════════════════════════════════════════════════════
# ValueProxy lazy child resolution
# ════════════════════════════════════════════════════════════════════


class TestLazyChildProxy:
    """Verify child proxies resolve via attr/list_get, not parent force."""

    def _worker(self) -> MagicMock:
        return _mock_reserved_worker()

    def _owner(self, active: list[bool] | None = None) -> _EvalOwner:
        return _EvalOwner(_EvalOwnerToken(), active)

    def _child_proxy(
        self,
        worker: MagicMock,
        parent: ValueProxy | _ResolvedValue,
        selector: str | int,
        *,
        owner: _EvalOwner | None = None,
        timeout: float | None = None,
    ) -> ValueProxy:
        return _EvalProxyContext(EvalProxy(worker), owner or self._owner(), timeout).child(parent, selector)

    async def test_attrs_getitem_force_calls_attr(self):
        """attrs[\"x\"].force() calls eval.attr with parent handle and name."""
        w = self._worker()
        w._eval_stub.attr.return_value = _mock_value_handle(5, "int")
        w._eval_stub.force.return_value = _mock_force_value_scalar(99)
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), "name")

        result = await cp.force()

        assert w._eval_stub.attr.await_count == 1
        assert w._eval_stub.force.await_count == 1
        assert result == 99

    async def test_list_getitem_force_calls_list_get(self):
        """lst[0].force() calls eval.list_get with parent handle and index."""
        w = self._worker()
        w._eval_stub.list_get.return_value = _mock_value_handle(3, "int")
        w._eval_stub.force.return_value = _mock_force_value_scalar(42)
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), 0)

        result = await cp.force()

        assert w._eval_stub.list_get.await_count == 1
        assert w._eval_stub.force.await_count == 1
        assert result == 42

    async def test_no_rpc_until_force(self):
        """No RPC is made until .force() is called on the child proxy."""
        w = self._worker()
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), "name")
        w._eval_stub.attr.assert_not_called()
        # accessing a property doesn't trigger RPC either
        with pytest.raises(UnresolvedValueError, match="not been resolved"):
            _ = cp.handle
        w._eval_stub.attr.assert_not_called()

    async def test_child_proxy_force_deep(self):
        """force_deep resolves child then deep-forces it."""
        w = self._worker()
        w._eval_stub.attr.return_value = _mock_value_handle(5, "attrs")
        from nanopynix_proto.nix.common import DeepAttrs, DeepValue, ScalarValue

        deep_val = DeepValue(
            attrs=DeepAttrs(
                entries={
                    "a": DeepValue(scalar=ScalarValue(int_value=1)),
                    "b": DeepValue(scalar=ScalarValue(int_value=2)),
                }
            )
        )
        w._eval_stub.force_deep.return_value = deep_val
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), "name")

        result = await cp.force_deep()

        assert w._eval_stub.attr.await_count == 1
        assert w._eval_stub.force_deep.await_count == 1
        assert result == {"a": 1, "b": 2}

    async def test_child_proxy_inactive_raises(self):
        """Child proxy raises after session close."""
        w = self._worker()
        active = [False]
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), "name", owner=self._owner(active))

        with pytest.raises(EvalSessionClosedError, match="EvalSession has been closed"):
            await cp.force()

    async def test_child_proxy_timeout_override(self):
        """Timeout override is passed through to resolve and force."""
        w = self._worker()
        w._eval_stub.attr.return_value = _mock_value_handle(5, "int")
        w._eval_stub.force.return_value = _mock_force_value_scalar(42)
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), "name", timeout=30.0)

        await cp.force(timeout=10.0)

        assert w._eval_stub.attr.call_args[1]["timeout"] == _RPC_TIMEOUT  # type: ignore[reportUnknownMemberType, reportOptionalSubscript] -- mock call_args absence in stubs


# ════════════════════════════════════════════════════════════════════
# ReservedWorker
# ════════════════════════════════════════════════════════════════════


class TestReservedWorker:
    async def test_release_idempotent(self):
        manager = MagicMock(spec=_WorkerManager)
        manager._release = MagicMock()

        rw = ReservedWorker(manager)
        await rw.release()
        manager._release.assert_called_once()

        # Second release should be a no-op
        await rw.release()
        manager._release.assert_called_once()  # still only once

    async def test_eval_stub_property_delegates(self):
        """ReservedWorker._eval_stub delegates to manager._eval_stub."""
        manager = MagicMock(spec=_WorkerManager)
        manager._eval_stub = _make_eval_stub()
        rw = ReservedWorker(manager)
        assert rw._eval_stub is manager._eval_stub  # type: ignore[reportPrivateUsage] -- cross-class access in test

    async def test_store_stub_property_delegates(self):
        """ReservedWorker._store_stub delegates to manager._store_stub."""
        manager = MagicMock(spec=_WorkerManager)
        manager._store_stub = MagicMock()
        rw = ReservedWorker(manager)
        assert rw._store_stub is manager._store_stub  # type: ignore[reportPrivateUsage] -- cross-class access in test

    async def test_call_serializes_reserved_worker_rpcs(self):
        """Concurrent calls through one reserved worker run one at a time."""
        manager = MagicMock(spec=_WorkerManager)
        rw = ReservedWorker(manager)
        first_started = asyncio.Event()
        first_can_finish = asyncio.Event()
        second_started = asyncio.Event()
        order: list[str] = []

        async def first():
            first_started.set()
            order.append("first-start")
            await first_can_finish.wait()
            order.append("first-end")
            return "first"

        async def second():
            second_started.set()
            order.append("second")
            return "second"

        first_task = asyncio.create_task(rw.call(first()))
        await first_started.wait()
        second_task = asyncio.create_task(rw.call(second()))
        await asyncio.sleep(0)

        assert not second_started.is_set()

        first_can_finish.set()
        assert await first_task == "first"
        assert await second_task == "second"
        assert order == ["first-start", "first-end", "second"]


class TestWorkerOomScore:
    def test_write_oom_score_adj_clamps_value(self, tmp_path: Path):
        proc_dir = tmp_path / "123"
        proc_dir.mkdir()

        pool_module._write_oom_score_adj(123, 2000, proc_root=tmp_path)  # type: ignore[reportPrivateUsage] -- test accesses pool internals

        assert (proc_dir / "oom_score_adj").read_text() == "1000\n"

    def test_worker_start_sets_base_oom_score(self, monkeypatch: pytest.MonkeyPatch):
        calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            pool_module,
            "_write_oom_score_adj",
            lambda pid, value: calls.append((pid, value)),
        )
        manager = _WorkerManager(worker_oom_score_adj=500)  # type: ignore[reportPrivateUsage] -- test accesses pool internals

        manager._on_worker_process_start(MagicMock(pid=1234))  # type: ignore[reportPrivateUsage] -- test accesses pool internals

        assert manager._worker_pid == 1234  # type: ignore[reportPrivateUsage] -- test accesses pool internals
        assert calls == [(1234, 500)]

    async def test_reserved_worker_score_is_restored_on_release(self, monkeypatch: pytest.MonkeyPatch):
        calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            pool_module,
            "_write_oom_score_adj",
            lambda pid, value: calls.append((pid, value)),
        )
        manager = _WorkerManager(  # type: ignore[reportPrivateUsage] -- test accesses pool internals
            worker_oom_score_adj=500,
            reserved_worker_oom_score_adj=250,
        )
        manager._channel = cast("Any", object())  # type: ignore[reportPrivateUsage] -- test accesses pool internals
        manager._worker_pid = 1234  # type: ignore[reportPrivateUsage] -- test accesses pool internals

        worker = await manager.reserve()
        await worker.release()

        assert calls == [(1234, 250), (1234, 500)]


# ════════════════════════════════════════════════════════════════════
# log_stream request-id handling (C1 fix)
# ════════════════════════════════════════════════════════════════════


class TestLogStreamRequestId:
    """Verify Session.log_stream() correctly maps worker wire format to LogEvent."""

    @staticmethod
    def _events_to_log_stream(events: list[Any]):
        """Return an async generator that yields the given events."""

        async def _gen():
            for e in events:
                yield e

        return _gen()

    @staticmethod
    def _proto_log_event(request_id: int, action: str, args: list[Any], result_type: int | None = None) -> LogEventProto:
        import json as _json

        le = MagicMock(spec=LogEventProto)
        le.request_id = request_id
        le.action = action
        le.args_json = _json.dumps(args)
        le.result_type = result_type
        return le

    async def test_worker_request_id_mapped_correctly(self):
        """Worker emits LogEvent proto — log_stream produces valid LogEvent."""
        from nanopynix.nix import Session

        session = object.__new__(Session)
        manager = MagicMock()
        manager.log_stream = MagicMock(
            return_value=self._events_to_log_stream(
                [
                    self._proto_log_event(42, "msg", [3, "hello from nix"]),
                    self._proto_log_event(7, "start", [0, "building"]),
                    self._proto_log_event(7, "result", [0, 100], result_type=100),
                ]
            )
        )
        session._manager = manager  # type: ignore[reportPrivateUsage] -- test injects mock manager

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

    async def test_none_sentinel_skipped(self):
        """None sentinel from log_stream is skipped."""
        from nanopynix.nix import Session

        session = object.__new__(Session)
        manager = MagicMock()
        manager.log_stream = MagicMock(
            return_value=self._events_to_log_stream(
                [
                    None,
                    self._proto_log_event(1, "msg", [3, "after sentinel"]),
                ]
            )
        )
        session._manager = manager  # type: ignore[reportPrivateUsage] -- test injects mock manager

        events = [e async for e in session.log_stream()]
        assert len(events) == 1
        assert events[0].request_id == 1


class TestLogCapture:
    async def test_capture_records_typed_events(self):
        session = object.__new__(Session)
        manager = _WorkerManager()
        session._manager = manager  # type: ignore[reportPrivateUsage] -- test injects mock manager

        async with session.capture_logs() as logs:
            manager._log_bus.emit(self._proto_log_event(4, "msg", [3, "hello"]))  # type: ignore[reportPrivateUsage] -- test accesses internal log bus
            manager._log_bus.emit(self._proto_log_event(4, "result", [1, 100], result_type=100))  # type: ignore[reportPrivateUsage] -- test accesses internal log bus

        assert [event.action for event in logs.events] == ["msg", "result"]
        assert logs.events[0].request_id == 4
        assert logs.events[0].args == [3, "hello"]
        assert logs.events[1].result_type == 100

    async def test_capture_unsubscribes_on_exit(self):
        session = object.__new__(Session)
        manager = _WorkerManager()
        session._manager = manager  # type: ignore[reportPrivateUsage] -- test injects mock manager

        async with session.capture_logs() as logs:
            manager._log_bus.emit(self._proto_log_event(1, "msg", ["inside"]))  # type: ignore[reportPrivateUsage] -- test accesses internal log bus

        manager._log_bus.emit(self._proto_log_event(1, "msg", ["outside"]))  # type: ignore[reportPrivateUsage] -- test accesses internal log bus

        assert len(logs.events) == 1
        assert logs.events[0].args == ["inside"]

    @staticmethod
    def _proto_log_event(request_id: int, action: str, args: list[Any], result_type: int | None = None) -> LogEventProto:
        import json as _json

        le = MagicMock(spec=LogEventProto)
        le.request_id = request_id
        le.action = action
        le.args_json = _json.dumps(args)
        le.result_type = result_type
        return le


# Session is imported at the top of the module
