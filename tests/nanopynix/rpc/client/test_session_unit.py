"""Unit tests for EvalSession + ValueProxy lifecycle using mocks.

No Nix daemon needed — exercises error paths and edge cases.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownLambdaType=false, reportUnknownArgumentType=false
# The entire file exercises pool/session internals via mock access.
# All members and variables accessed on MagicMock objects are inherently unknown.

from __future__ import annotations

import asyncio
import copy
import gc
import json as _json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import anyio.lowlevel
import pytest
from nanopynix_proto.nix.common import LogEvent as LogEventProto, NixLogEvent, ResultType, ScalarValue
from nanopynix_proto.nix.eval import ForceJsonResponse

import nanopynix.rpc.client._pool as pool_module
from nanopynix import (
    BuildMode,
    EvalSessionClosedError,
    ForeignValueError,
    NixType,
    UnresolvedValueError,
    ValueReleasedError,
)
from nanopynix.models import LogEvent
from nanopynix.rpc.client._pool import WorkerClient
from nanopynix.rpc.client._session import (
    EvalProxy,
    EvalSession,
    ReplSession,
    ValueProxy,
    _EvalOwner as _EvalOwner,
    _EvalOwnerToken as _EvalOwnerToken,
    _EvalProxyContext as _EvalProxyContext,
    _ResolvedValue as _ResolvedValue,
)
from nanopynix.rpc.client.session import Session
from nanopynix.rpc.client.store import Store, StoreHandle
from nanopynix.settings import (
    DEFAULT_RPC_TIMEOUT_SECONDS,
    NixEvalSettings,
    NixFetchSettings,
    NixFlakeSettings,
)

if TYPE_CHECKING:
    from pathlib import Path

# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════


def _make_eval_stub() -> MagicMock:
    """Create a MagicMock that acts as an EvalServiceStub."""
    stub = MagicMock()
    stub.eval_file = AsyncMock()
    stub.eval_string = AsyncMock()
    stub.open_eval = AsyncMock()
    stub.close_eval = AsyncMock()
    stub.begin_repl = AsyncMock()
    stub.repl_process_line = AsyncMock()
    stub.as_scalar = AsyncMock()
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
    return stub


def _mock_value_handle(handle: int = 1, type_str: str = "int") -> MagicMock:
    """Return a MagicMock that looks like a ValueHandle proto."""
    vh = MagicMock()
    vh.handle = handle
    vh.type = type_str
    return vh


def _mock_scalar(value: Any) -> MagicMock:
    """Return a Scalar proto mock -- what the AsScalar RPC answers with.

    This used to build a ``ForceValue`` wrapper for the Force RPC, which is
    gone -- the wire op went the way of the client's ``force()``. AsScalar
    answers with the scalar itself, so the wrapper went with it.
    """
    scalar = MagicMock(spec=ScalarValue)
    scalar.string_value = value if isinstance(value, str) else None
    scalar.int_value = value if isinstance(value, int) and not isinstance(value, bool) else None
    scalar.float_value = value if isinstance(value, float) else None
    scalar.bool_value = value if isinstance(value, bool) else None
    scalar.null_value = MagicMock() if value is None else None
    return scalar


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


def _mock_pool() -> MagicMock:
    """Return a mock worker client that serializes individual RPCs."""
    pool = MagicMock(spec=WorkerClient)
    pool.eval_stub = _make_eval_stub()
    pool.store_stub = MagicMock()
    pool.rpc_timeout = DEFAULT_RPC_TIMEOUT_SECONDS

    async def _invoke(method: Any, request: Any, *, timeout: float) -> Any:  # noqa: ASYNC109 -- mock implementing WorkerClient.invoke; timeout passed to grpclib stub
        return await method(request, timeout=timeout)

    pool.invoke = _invoke
    return pool


def _mock_worker_client() -> MagicMock:
    """Return a mock worker client with eval and Store RPC stubs."""
    rw = MagicMock(spec=WorkerClient)
    rw.eval_stub = _make_eval_stub()
    rw.store_stub = MagicMock()
    rw.rpc_timeout = DEFAULT_RPC_TIMEOUT_SECONDS
    rw.store_stub.build_paths_with_results = AsyncMock()
    rw.store_stub.read_derivation = AsyncMock()
    rw.release = AsyncMock()

    async def _invoke(method: Any, request: Any, *, timeout: float) -> Any:  # noqa: ASYNC109 -- mock implementing WorkerClient.invoke; timeout passed to grpclib stub
        return await method(request, timeout=timeout)

    rw.invoke = _invoke
    return rw


# ════════════════════════════════════════════════════════════════════
# EvalSession lifecycle
# ════════════════════════════════════════════════════════════════════


class TestEvalSessionLifecycle:
    async def test_enter_opens_eval_state_on_worker_client(self):
        pool = _mock_pool()

        session = EvalSession(pool)
        result = await session.__aenter__()
        assert result is session
        pool.eval_stub.open_eval.assert_awaited_once()

    async def test_open_close_manual_lifecycle(self):
        pool = _mock_pool()
        session = EvalSession(pool)
        await session.open()
        await session.close()

        pool.eval_stub.close_eval.assert_awaited_once()

    async def test_exit_closes_eval_state(self):
        pool = _mock_pool()

        session = EvalSession(pool)
        await session.__aenter__()
        await session.__aexit__(None, None, None)

        pool.eval_stub.close_eval.assert_awaited_once()

    async def test_exit_deactivates_after_close_eval_error(self):
        """A failed CloseEval still invalidates the client-side session."""
        pool = _mock_pool()
        pool.eval_stub.close_eval.side_effect = TimeoutError("close_eval timed out")

        session = EvalSession(pool)
        await session.__aenter__()
        with pytest.raises(TimeoutError, match="close_eval timed out"):
            await session.__aexit__(None, None, None)

        with pytest.raises(EvalSessionClosedError, match="not entered"):
            await session.string("1")

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
        pool.eval_stub.eval_file.return_value = _mock_value_handle(1, "attrs")

        session = EvalSession(pool)
        await session.__aenter__()
        root = await session.file("/some/path.nix")
        assert isinstance(root, ValueProxy)
        assert root.handle == 1
        assert root.nix_type == NixType.ATTRS
        request = pool.eval_stub.open_eval.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert request.store_handle == 1

    async def test_string_after_enter(self):
        pool = _mock_pool()
        pool.eval_stub.eval_string.return_value = _mock_value_handle(2, "int")

        session = EvalSession(pool, store_handle=99)
        await session.__aenter__()
        root = await session.string("42 + 1")
        assert root.nix_type == NixType.INT
        request = pool.eval_stub.open_eval.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert request.store_handle == 99

    async def test_open_eval_carries_the_build_store_handle(self):
        """``Session.eval(store, build_store=...)`` must put the handle on the wire.

        The client half of the ``build_store`` unification -- its worker half
        is ``test_worker_eval_unit.test_open_eval_forwards_the_build_store_handle``.
        Neither is redundant: this one would still pass if the worker dropped
        the field on the floor, and that one would still pass if the client
        never set it.
        """
        pool = _mock_pool()
        rpc = StoreHandle(_mock_pool(), "mock", "session-id")
        rpc._active = True  # type: ignore[reportPrivateUsage] -- test injects internal store state
        rpc._store_handle = 456  # type: ignore[reportPrivateUsage] -- test injects store handle directly for mock setup

        session = EvalSession(pool, store_handle=99, build_store=Store(rpc))
        await session.open()
        request = pool.eval_stub.open_eval.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert request.store_handle == 99
        assert request.build_store_handle == 456

    async def test_open_eval_sends_zero_when_no_build_store(self):
        """0 is the wire's "none", as for every other optional handle."""
        pool = _mock_pool()
        session = EvalSession(pool, store_handle=99)
        await session.open()
        request = pool.eval_stub.open_eval.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert request.build_store_handle == 0

    @pytest.mark.concurrency
    async def test_eval_proxy_serializes_concurrent_operations(self):
        """One EvalState never receives overlapping RPCs from its proxies."""
        pool = _mock_pool()
        started = anyio.Event()
        unblock = anyio.Event()
        active = 0
        peak_active = 0

        async def eval_string(_request: Any, **_kwargs: Any) -> MagicMock:
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            started.set()
            await unblock.wait()
            active -= 1
            return _mock_value_handle(active + 1, "int")

        pool.eval_stub.eval_string.side_effect = eval_string
        session = EvalSession(pool)
        await session.open()
        try:
            first = asyncio.create_task(session.string("1"))
            await started.wait()
            second = asyncio.create_task(session.string("2"))
            await anyio.lowlevel.checkpoint()
            assert peak_active == 1
            unblock.set()
            await asyncio.gather(first, second)
            assert peak_active == 1
        finally:
            await session.close()

    async def test_timeout_override(self):
        pool = _mock_pool()
        pool.eval_stub.eval_string.return_value = _mock_value_handle(1, "int")

        session = EvalSession(pool, timeout=10.0)
        await session.__aenter__()
        await session.string("42", timeout=5.0)
        call_kwargs = pool.eval_stub.eval_string.call_args[1]  # type: ignore[reportUnknownMemberType, reportOptionalSubscript] -- mock call_args absence in stubs
        assert call_kwargs["timeout"] == DEFAULT_RPC_TIMEOUT_SECONDS

    async def test_timeout_falls_back_to_session_default(self):
        pool = _mock_pool()
        pool.eval_stub.eval_string.return_value = _mock_value_handle(1, "int")

        session = EvalSession(pool, timeout=10.0)
        await session.__aenter__()
        await session.string("42")  # no override
        call_kwargs = pool.eval_stub.eval_string.call_args[1]  # type: ignore[reportUnknownMemberType, reportOptionalSubscript] -- mock call_args absence in stubs
        assert call_kwargs["timeout"] == DEFAULT_RPC_TIMEOUT_SECONDS


class TestReplSession:
    async def test_open_starts_repl_scope_and_line_returns_expression_value(self):
        pool = _mock_pool()
        pool.eval_stub.repl_process_line.return_value = SimpleNamespace(
            is_binding=False,
            value=_mock_value_handle(7, "int"),
        )

        session = ReplSession(pool, store_handle=99)
        await session.open()
        result = await session.line("x + 1")

        pool.eval_stub.begin_repl.assert_awaited_once()
        open_request = pool.eval_stub.open_eval.call_args.args[0]
        assert open_request.store_handle == 99
        assert isinstance(result, ValueProxy)
        assert result.handle == 7

    async def test_line_returns_none_for_binding(self):
        pool = _mock_pool()
        pool.eval_stub.repl_process_line.return_value = SimpleNamespace(is_binding=True)

        session = ReplSession(pool)
        await session.open()

        assert await session.line("x = 1") is None


class TestSessionEvalFacade:
    def _session(self) -> Session:
        session = object.__new__(Session)
        session._manager = _mock_pool()  # type: ignore[reportPrivateUsage] -- test injects mock manager
        session._session_id = "session-id"  # type: ignore[reportPrivateUsage] -- test injects internal session ID
        # `eval()` merges the session's defaults for the three evaluator-facing
        # scopes under the per-call argument. Empty models here: these tests are
        # about the store handle and the ownership guard, so the defaults must
        # add nothing. The routing itself is covered in test_config_flow.py.
        session._eval_defaults = NixEvalSettings()  # type: ignore[reportPrivateUsage] -- test injects session-scoped defaults
        session._fetch_defaults = NixFetchSettings()  # type: ignore[reportPrivateUsage] -- test injects session-scoped defaults
        session._flake_defaults = NixFlakeSettings()  # type: ignore[reportPrivateUsage] -- test injects session-scoped defaults
        return session

    def _store(self, session_id: str = "session-id", handle: int = 42) -> Store:
        rpc = StoreHandle(_mock_pool(), "mock", session_id)
        rpc._active = True  # type: ignore[reportPrivateUsage] -- test injects internal store state
        rpc._store_handle = handle  # type: ignore[reportPrivateUsage] -- test injects store handle directly for mock setup
        return Store(rpc)

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
        store = Store(StoreHandle(_mock_pool(), "mock", "session-id"))

        with pytest.raises(RuntimeError, match="StoreHandle is closed"):
            session.eval(store)


# ════════════════════════════════════════════════════════════════════
# ValueProxy lifecycle
# ════════════════════════════════════════════════════════════════════


class TestValueProxyLifecycle:
    def _worker(self) -> MagicMock:
        return _mock_worker_client()

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

    async def test_as_int_delegates_to_worker(self):
        w = self._worker()
        w.eval_stub.as_scalar.return_value = _mock_scalar(99)
        vp = self._proxy(w, 1, "int")
        result = await vp.as_int()
        assert result == 99

    # Two force_as unit tests lived here -- that it read the cached type
    # rather than round-tripping, and that a wrong type raised
    # WrongNixTypeError without touching the worker. Both described
    # client-side type checking, which is exactly what force_as was deleted
    # for: as_int() asks the worker, so Nix decides and both engines raise the
    # same NixTypeError. tests/nanopynix/test_scalar_accessor_semantics.py
    # covers the replacement against real values on both engines.

    async def test_call_json_arg_uses_explicit_wire_arg(self):
        w = self._worker()
        w.eval_stub.call.return_value = _mock_value_handle(3, "int")
        vp = self._proxy(w, 1, "function")

        result = await vp({"name": "demo"})

        assert result.handle == 3

    async def test_call_does_not_pre_check_the_type_client_side(self):
        """Calling a non-function goes to the worker and lets Nix reject it.

        There used to be a proxy-side guard here that compared the cached type
        and raised ``WrongNixTypeError`` without ever calling. It was removed:
        ``WrongNixTypeError`` is an ``ObjectMisuseError``, unrelated to the
        ``NixTypeError`` inproc raises for the same mistake, so no single
        ``except`` covered both engines -- and the message the guard produced
        was ours rather than Nix's.
        """
        w = self._worker()
        w.eval_stub.call.return_value = _mock_value_handle(3, "int")
        vp = self._proxy(w, 1, "int")

        # An argument, because a nullary call is refused before dispatch on
        # both engines -- Nix has no `f ()`. Passing one is what makes this
        # test about the *type* pre-check it is named for.
        await vp(1)

        w.eval_stub.call.assert_awaited_once()

    async def test_call_value_proxy_arg_uses_remote_handle(self):
        w = self._worker()
        w.eval_stub.call.return_value = _mock_value_handle(3, "int")
        owner = self._owner()
        fn = self._proxy(w, 1, "function", owner=owner)
        arg = self._proxy(w, 2, "attrs", owner=owner)

        result = await fn(arg)

        assert result.handle == 3
        w.eval_stub.call.assert_awaited_once()

    async def test_call_nested_value_proxy_arg_uses_remote_handle(self):
        w = self._worker()
        w.eval_stub.call.return_value = _mock_value_handle(3, "int")
        owner = self._owner()
        fn = self._proxy(w, 1, "function", owner=owner)
        arg = self._proxy(w, 2, "attrs", owner=owner)

        result = await fn({"items": [arg, 1]})

        assert result.handle == 3
        w.eval_stub.call.assert_awaited_once()

    async def test_call_foreign_value_proxy_raises_typed_error(self):
        w = self._worker()
        fn = self._proxy(w, 1, "function", owner=self._owner())
        arg = self._proxy(w, 2, "attrs", owner=self._owner())

        with pytest.raises(ForeignValueError, match="another EvalSession"):
            await fn(arg)

        w.eval_stub.call.assert_not_awaited()

    async def test_call_nested_foreign_value_proxy_raises_typed_error(self):
        w = self._worker()
        fn = self._proxy(w, 1, "function", owner=self._owner())
        arg = self._proxy(w, 2, "attrs", owner=self._owner())

        with pytest.raises(ForeignValueError, match="another EvalSession"):
            await fn({"arg": [arg]})

        w.eval_stub.call.assert_not_awaited()

    async def test_attr_returns_new_proxy(self):
        w = self._worker()
        vp = self._proxy(w, 1, "attrs")
        child = vp.attr("name")
        assert isinstance(child, ValueProxy)
        assert child.nix_type == NixType.UNSPECIFIED
        w.eval_stub.attr.assert_not_awaited()

        w.eval_stub.attr.return_value = _mock_value_handle(5, "string")
        w.eval_stub.as_scalar.return_value = _mock_scalar("hello")
        assert await child.as_string() == "hello"
        assert child.handle == 5
        assert child.nix_type == NixType.STRING

    async def test_list_get_returns_new_proxy(self):
        w = self._worker()
        vp = self._proxy(w, 1, "list")
        child = vp.list_get(0)
        w.eval_stub.list_get.assert_not_awaited()

        w.eval_stub.list_get.return_value = _mock_value_handle(3, "int")
        w.eval_stub.type_name.return_value = _mock_type_name_response("int")
        assert await child.get_type() == NixType.INT
        assert child.handle == 3

    async def test_list_get_accepts_negative_index(self):
        w = self._worker()
        vp = self._proxy(w, 1, "list")

        # Negative indices are deferred to the worker (normalised against list_length).
        child = vp.list_get(-1)
        assert child is not None
        # The stub should not be called yet — list_get is lazy.
        w.eval_stub.list_get.assert_not_awaited()

    async def test_list_length(self):
        w = self._worker()
        w.eval_stub.list_length.return_value = _mock_list_length_response(3)
        vp = self._proxy(w, 1, "list")
        assert await vp.list_length() == 3

    async def test_attr_names(self):
        w = self._worker()
        w.eval_stub.attr_names.return_value = _mock_attr_names_response(["a", "b", "c"])
        vp = self._proxy(w, 1, "attrs")
        assert await vp.attr_names() == ["a", "b", "c"]

    async def test_has_attr(self):
        w = self._worker()
        w.eval_stub.has_attr.return_value = _mock_has_attr_response(True)
        vp = self._proxy(w, 1, "attrs")
        assert await vp.has_attr("foo") is True

    async def test_build_uses_cascading_build_by_default(self):
        w = self._worker()
        w.eval_stub.build.return_value = _mock_build_response()
        vp = _EvalProxyContext(EvalProxy(w), self._owner(), store_handle=123).value(1, "attrs")

        result = await vp.build()

        assert result == {"out": "/nix/store/aaa-demo"}
        w.eval_stub.build.assert_awaited_once()
        build_request = w.eval_stub.build.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert build_request.handle == 1
        assert build_request.build_mode == BuildMode.Normal.value
        assert build_request.build_store_handle == 0
        w.eval_stub.attr.assert_not_awaited()
        w.eval_stub.force_json.assert_not_awaited()
        w.store_stub.build_paths_with_results.assert_not_awaited()
        w.store_stub.read_derivation.assert_not_awaited()

    async def test_build_mode_uses_cascading_build(self):
        w = self._worker()
        w.eval_stub.build.return_value = _mock_build_response()
        vp = _EvalProxyContext(EvalProxy(w), self._owner(), store_handle=123).value(1, "attrs")

        result = await vp.build(build_mode=BuildMode.Check)

        assert result == {"out": "/nix/store/aaa-demo"}
        w.eval_stub.build.assert_awaited_once()
        build_request = w.eval_stub.build.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert build_request.handle == 1
        assert build_request.build_mode == BuildMode.Check.value
        assert build_request.build_store_handle == 0
        w.store_stub.build_paths_with_results.assert_not_awaited()

    async def test_build_store_overrides_build_store_not_eval_store(self):
        w = self._worker()
        w.eval_stub.build.return_value = _mock_build_response()
        rpc = StoreHandle(_mock_pool(), "mock", "session-id")
        rpc._active = True  # type: ignore[reportPrivateUsage] -- test injects internal store state
        rpc._store_handle = 456  # type: ignore[reportPrivateUsage] -- test injects store handle directly for mock setup
        build_store = Store(rpc)
        ctx = _EvalProxyContext(EvalProxy(w), self._owner(), store_handle=123, session_id="session-id")
        vp = ctx.value(1, "attrs")

        result = await vp.build(store=build_store)

        assert result == {"out": "/nix/store/aaa-demo"}
        build_request = w.eval_stub.build.call_args.args[0]  # type: ignore[reportUnknownMemberType, reportOptionalMemberAccess] -- mock call_args absence in stubs
        assert build_request.handle == 1
        assert build_request.build_store_handle == 456

    async def test_build_rejects_foreign_build_store(self):
        w = self._worker()
        rpc = StoreHandle(_mock_pool(), "mock", "other-session")
        rpc._active = True  # type: ignore[reportPrivateUsage] -- test injects internal store state
        rpc._store_handle = 456  # type: ignore[reportPrivateUsage] -- test injects store handle directly for mock setup
        build_store = Store(rpc)
        ctx = _EvalProxyContext(EvalProxy(w), self._owner(), store_handle=123, session_id="session-id")
        vp = ctx.value(1, "attrs")

        with pytest.raises(ValueError, match="different session"):
            await vp.build(store=build_store)

    async def test_get_type_delegates_to_worker_for_thunk(self):
        w = self._worker()
        w.eval_stub.type_name.return_value = _mock_type_name_response("attrs")
        vp = self._proxy(w, 1, "thunk")
        assert await vp.get_type() == NixType.ATTRS
        assert vp.nix_type == NixType.ATTRS
        w.eval_stub.type_name.assert_awaited_once()

    async def test_get_type_uses_cached_concrete_type(self):
        w = self._worker()
        vp = self._proxy(w, 1, "attrs")

        assert await vp.get_type() == NixType.ATTRS
        w.eval_stub.type_name.assert_not_awaited()

    async def test_release(self):
        w = self._worker()
        w.eval_stub.release.return_value = MagicMock()
        vp = self._proxy(w, 1, "int")
        await vp.release()
        w.eval_stub.release.assert_awaited_once()

    async def test_raises_after_session_close(self):
        w = self._worker()
        active = [True]
        vp = self._proxy(w, 1, "int", owner=self._owner(active))

        # Active — works
        w.eval_stub.as_scalar.return_value = _mock_scalar(42)
        assert await vp.as_int() == 42

        # Session closed
        active[0] = False
        with pytest.raises(EvalSessionClosedError, match="EvalSession has been closed"):
            await vp.as_int()

    async def test_release_then_read_raises_typed_error(self):
        w = self._worker()
        w.eval_stub.release.return_value = MagicMock()
        vp = self._proxy(w, 1, "int")
        await vp.release()

        with pytest.raises(ValueReleasedError, match="has been released"):
            await vp.as_int()

    async def test_unresolved_as_dict_children_die_with_their_parent(self):
        """A lazy child cannot outlive the parent it has yet to resolve against.

        This replaces a test that ``ValueAttrs`` could not independently
        release its parent's handle. ``as_dict()`` hands back plain lazy
        ``ValueProxy`` children instead of a view, so the property to check
        is now the child's: until it resolves it holds only a reference to
        the parent, and a released parent must make that a typed error
        rather than an RPC with a dead handle.
        """
        w = self._worker()
        w.eval_stub.attr_names.return_value = MagicMock(names=["x"])
        w.eval_stub.release.return_value = MagicMock()
        parent = self._proxy(w, 1, "attrs")

        attrs = await parent.as_dict()
        child = attrs["x"]

        await parent.release()
        with pytest.raises(ValueReleasedError, match="ValueProxy has been released"):
            await child.as_int()
        w.eval_stub.release.assert_awaited_once()

    async def test_finalizer_defers_release_until_a_safe_rpc_boundary(self):
        w = self._worker()
        proxy = EvalProxy(w)
        ctx = _EvalProxyContext(proxy, self._owner())
        value = ctx.value(7, "int")

        del value
        gc.collect()

        w.eval_stub.release.assert_not_awaited()
        await proxy.drain_deferred_releases()
        w.eval_stub.release.assert_awaited_once()

    async def test_value_proxy_rejects_copying(self):
        value = self._proxy(self._worker(), 7, "int")

        with pytest.raises(TypeError, match="cannot be copied"):
            copy.copy(value)
        with pytest.raises(TypeError, match="cannot be copied"):
            copy.deepcopy(value)

    async def test_check_active_only_when_owner_has_flag(self):
        """An owner without an active flag never expires."""
        w = self._worker()
        w.eval_stub.as_scalar.return_value = _mock_scalar(42)
        vp = self._proxy(w, 1, "int")
        assert await vp.as_int() == 42  # should not raise

    async def test_handle_still_accessible_after_close(self):
        """Cached properties are available even after session close."""
        w = self._worker()
        active = [True]
        vp = self._proxy(w, 42, "attrs", owner=self._owner(active))
        active[0] = False
        assert vp.handle == 42
        assert vp.nix_type == NixType.ATTRS


# `TestValueListBounds` lived here: two tests that `ValueList[i]` raised
# `IndexError` for an out-of-range index, positive or negative, without
# reaching the worker. `as_list()` returns a real Python list of lazy
# proxies, so both properties are now Python's own -- the bounds check and
# the "no RPC for an index that cannot exist" guarantee come free from the
# list object, and there is no hand-rolled `_check_index` left to test.

# ════════════════════════════════════════════════════════════════════
# ValueProxy lazy child resolution
# ════════════════════════════════════════════════════════════════════


class TestLazyChildProxy:
    """Verify child proxies resolve via attr/list_get, not parent force."""

    def _worker(self) -> MagicMock:
        return _mock_worker_client()

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
        ctx = _EvalProxyContext(EvalProxy(worker), owner or self._owner(), timeout)
        parent_proxy = parent if isinstance(parent, ValueProxy) else ctx.value(parent.handle, parent.nix_type)
        return ctx.child(parent_proxy, selector)

    async def test_attrs_getitem_read_calls_attr(self):
        """Reading attrs["x"] calls eval.attr with the parent handle and name."""
        w = self._worker()
        w.eval_stub.attr.return_value = _mock_value_handle(5, "int")
        w.eval_stub.as_scalar.return_value = _mock_scalar(99)
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), "name")

        result = await cp.as_int()

        assert w.eval_stub.attr.await_count == 1
        assert w.eval_stub.as_scalar.await_count == 1
        assert result == 99

    async def test_list_getitem_read_calls_list_get(self):
        """Reading lst[0] calls eval.list_get with the parent handle and index."""
        w = self._worker()
        w.eval_stub.list_get.return_value = _mock_value_handle(3, "int")
        w.eval_stub.as_scalar.return_value = _mock_scalar(42)
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), 0)

        result = await cp.as_int()

        assert w.eval_stub.list_get.await_count == 1
        assert w.eval_stub.as_scalar.await_count == 1
        assert result == 42

    async def test_no_rpc_until_the_child_is_read(self):
        """No RPC is made until the child proxy is actually read."""
        w = self._worker()
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), "name")
        w.eval_stub.attr.assert_not_called()
        # accessing a property doesn't trigger RPC either
        with pytest.raises(UnresolvedValueError, match="not been resolved"):
            _ = cp.handle
        w.eval_stub.attr.assert_not_called()

    async def test_child_proxy_to_python(self):
        """to_python resolves the child, then converts it over the ForceJson RPC.

        The wire op is still ForceJson -- it transfers JSON and the client
        decodes it -- so the stub being driven here is ``force_json`` even
        though the caller-facing method is ``to_python``.
        """
        w = self._worker()
        w.eval_stub.attr.return_value = _mock_value_handle(5, "attrs")
        w.eval_stub.force_json.return_value = ForceJsonResponse(json='{"a": 1, "b": 2}')
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), "name")

        result = await cp.to_python()

        assert w.eval_stub.attr.await_count == 1
        assert w.eval_stub.force_json.await_count == 1
        assert result == {"a": 1, "b": 2}

    async def test_child_proxy_inactive_raises(self):
        """Child proxy raises after session close."""
        w = self._worker()
        active = [False]
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), "name", owner=self._owner(active))

        with pytest.raises(EvalSessionClosedError, match="EvalSession has been closed"):
            await cp.as_int()

    async def test_child_proxy_timeout_override(self):
        """Timeout override is passed through to resolve and read."""
        w = self._worker()
        w.eval_stub.attr.return_value = _mock_value_handle(5, "int")
        w.eval_stub.as_scalar.return_value = _mock_scalar(42)
        cp = self._child_proxy(w, _ResolvedValue(1, NixType.UNSPECIFIED), "name", timeout=30.0)

        await cp.as_int(timeout=10.0)

        assert w.eval_stub.attr.call_args[1]["timeout"] == DEFAULT_RPC_TIMEOUT_SECONDS  # type: ignore[reportUnknownMemberType, reportOptionalSubscript] -- mock call_args absence in stubs


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
        manager = WorkerClient(worker_oom_score_adj=500)

        manager._on_worker_process_start(MagicMock(pid=1234))  # type: ignore[reportPrivateUsage] -- test accesses pool internals

        assert manager._worker_pid == 1234  # type: ignore[reportPrivateUsage] -- test accesses pool internals
        assert calls == [(1234, 500)]


# ════════════════════════════════════════════════════════════════════
# log_stream request-id handling (C1 fix)
# ════════════════════════════════════════════════════════════════════


class TestLogStreamRequestId:
    """What ``Session.log_stream()`` delivers, driven through the real bus.

    These tests used to inject a ``MagicMock`` manager that yielded protos,
    because ``Session.log_stream`` converted them itself. ``CallbackBus.emit``
    normalises now and ``Session.log_stream`` is a delegate, so a mocked
    manager would test the mock. A real ``WorkerClient`` costs nothing here --
    its bus needs no worker process -- and it covers the whole path.
    """

    @staticmethod
    def _proto_log_event(
        request_id: int, action: str, args: list[Any], result_type: int | None = None
    ) -> LogEventProto:
        return LogEventProto(
            request_id=request_id,
            nix_log=NixLogEvent(
                action=action,
                args_json=_json.dumps(args),
                result_type=ResultType(result_type) if result_type is not None else None,
            ),
        )

    async def _drain(self, manager: WorkerClient, events: list[LogEventProto | None]) -> list[LogEvent]:
        """Emit *events* onto the bus while one ``log_stream`` iterates it.

        The list must carry the ``None`` marker, or the iterator never stops
        and the task group waits for it.
        """
        session = object.__new__(Session)
        session._manager = manager  # type: ignore[reportPrivateUsage] -- test injects a real, unopened manager

        async def _emit() -> None:
            # One checkpoint first: the bus discards an event that arrives
            # with nobody subscribed, and the iterator below subscribes on its
            # first `__anext__`.
            await anyio.lowlevel.checkpoint()
            for event in events:
                manager._log_bus.emit(event)  # type: ignore[reportPrivateUsage] -- test accesses internal log bus

        collected: list[LogEvent] = []
        async with anyio.create_task_group() as tg:
            tg.start_soon(_emit)
            collected.extend([event async for event in session.log_stream()])
        return collected

    async def test_worker_request_id_mapped_correctly(self):
        """The worker emits the proto, and the caller gets the model."""
        events = await self._drain(
            WorkerClient(),
            [
                self._proto_log_event(42, "msg", [3, "hello from nix"]),
                self._proto_log_event(7, "start", [0, "building"]),
                self._proto_log_event(7, "result", [0, 100], result_type=100),
                None,
            ],
        )

        assert len(events) == 3
        for e in events:
            assert type(e) is LogEvent

        assert events[0].request_id == 42
        assert events[0].action == "msg"
        assert events[0].args == [3, "hello from nix"]
        assert events[0].result_type is None

        assert events[1].request_id == 7
        assert events[1].action == "start"

        assert events[2].request_id == 7
        assert events[2].action == "result"
        assert events[2].result_type == 100

    async def test_teardown_marker_ends_the_stream(self):
        """``None`` means the session closed, so the iterator stops there.

        It is not skipped. Skipping it left a caller waiting on a session that
        had already gone away -- see ``bus_log_stream``.
        """
        events = await self._drain(
            WorkerClient(),
            [
                self._proto_log_event(1, "msg", [3, "before close"]),
                None,
                self._proto_log_event(2, "msg", [3, "after close"]),
            ],
        )

        assert [e.request_id for e in events] == [1]


class TestLogCapture:
    async def test_capture_records_typed_events(self):
        session = object.__new__(Session)
        manager = WorkerClient()
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
        manager = WorkerClient()
        session._manager = manager  # type: ignore[reportPrivateUsage] -- test injects mock manager

        async with session.capture_logs() as logs:
            manager._log_bus.emit(self._proto_log_event(1, "msg", ["inside"]))  # type: ignore[reportPrivateUsage] -- test accesses internal log bus

        manager._log_bus.emit(self._proto_log_event(1, "msg", ["outside"]))  # type: ignore[reportPrivateUsage] -- test accesses internal log bus

        assert len(logs.events) == 1
        assert logs.events[0].args == ["inside"]

    @staticmethod
    def _proto_log_event(
        request_id: int, action: str, args: list[Any], result_type: int | None = None
    ) -> LogEventProto:
        return LogEventProto(
            request_id=request_id,
            nix_log=NixLogEvent(
                action=action,
                args_json=_json.dumps(args),
                result_type=ResultType(result_type) if result_type is not None else None,
            ),
        )
