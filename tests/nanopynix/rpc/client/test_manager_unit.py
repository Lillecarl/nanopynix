"""Unit coverage for nanopynix.rpc.client._manager.

This module is pure Python (no daemon/store/nix dependency): scalar
(de)serialization helpers and small grpclib ServiceBase classes. The
existing integration coverage in test_primop_rpc.py only exercises the
success path through a real Nix session, so it never reaches the base
classes' UNIMPLEMENTED stubs, the non-string/int/bool/float/null scalar
branches, or the NOT_FOUND / exception-wrapping error paths in
ManagerPrimopServiceHandler.call. These are "dumb coverage" tests: each
one pins down a single small branch directly, without an RPC round trip.
"""

from __future__ import annotations

from typing import Any

import grpclib
import pytest
from grpclib.const import Status
from nanopynix_proto.nix.common import DeepValue, LogEvent, NullValue, ScalarValue
from nanopynix_proto.nix.manager import CallPrimopRequest

from nanopynix.rpc.client._manager import (
    ManagerPrimopServiceBase,
    ManagerPrimopServiceHandler,
    ManagerServiceBase,
    ManagerServiceHandler,
    _python_to_scalar_value,  # pyright: ignore[reportPrivateUsage]
    _scalar_value_to_python,  # pyright: ignore[reportPrivateUsage]
)


class _FakeStream:
    """Minimal grpclib.server.Stream stand-in: records what was sent, replays one recv."""

    def __init__(self, to_recv: Any) -> None:
        self._to_recv = to_recv
        self.sent: list[Any] = []

    async def recv_message(self) -> Any:
        return self._to_recv

    async def send_message(self, message: Any) -> None:
        self.sent.append(message)


@pytest.mark.parametrize(
    ("scalar", "expected"),
    [
        (ScalarValue(string_value="hi"), "hi"),
        (ScalarValue(int_value=42), 42),
        (ScalarValue(float_value=1.5), 1.5),
        (ScalarValue(bool_value=True), True),
        (ScalarValue(null_value=NullValue()), None),
    ],
)
def test_scalar_value_to_python_covers_every_kind(scalar: ScalarValue, expected: Any) -> None:
    assert _scalar_value_to_python(scalar) == expected


@pytest.mark.parametrize(
    ("value", "field", "expected"),
    [
        (None, "null_value", NullValue()),
        (True, "bool_value", True),
        (42, "int_value", 42),
        (1.5, "float_value", 1.5),
        ("hi", "string_value", "hi"),
    ],
)
def test_python_to_scalar_value_covers_every_type(value: Any, field: str, expected: Any) -> None:
    sv = _python_to_scalar_value(value)
    assert getattr(sv, field) == expected


def test_python_to_scalar_value_rejects_unsupported_type() -> None:
    with pytest.raises(TypeError, match="unsupported RPC primop value type"):
        _python_to_scalar_value(object())


async def test_manager_service_base_log_is_unimplemented() -> None:
    """The base class exists to be subclassed; its own .log must reject calls."""
    with pytest.raises(grpclib.GRPCError) as exc_info:
        await ManagerServiceBase().log(LogEvent())
    assert exc_info.value.status == Status.UNIMPLEMENTED


async def test_manager_primop_service_base_call_is_unimplemented() -> None:
    with pytest.raises(grpclib.GRPCError) as exc_info:
        await ManagerPrimopServiceBase().call(CallPrimopRequest(name="whatever"))
    assert exc_info.value.status == Status.UNIMPLEMENTED


async def test_manager_service_handler_log_invokes_callback_and_acks() -> None:
    received: list[LogEvent] = []
    handler = ManagerServiceHandler(received.append)
    event = LogEvent()

    ack = await handler.log(event)

    assert received == [event]
    assert ack.ok is True


async def test_manager_service_base_rpc_log_roundtrips_through_stream() -> None:
    """Exercises the private __rpc_log stream handler registered in __mapping__."""
    received: list[LogEvent] = []
    handler = ManagerServiceHandler(received.append)
    route = "/nix.manager.ManagerService/Log"
    stream = _FakeStream(LogEvent())

    await handler.__mapping__()[route].func(stream)  # type: ignore[reportUnknownMemberType] -- grpclib.const.Handler.func has no typed stub

    assert len(received) == 1
    assert stream.sent[0].ok is True


def _discard_log_event(_event: LogEvent) -> None:
    return None


async def test_manager_service_base_rpc_log_rejects_missing_message() -> None:
    handler = ManagerServiceHandler(_discard_log_event)
    route = "/nix.manager.ManagerService/Log"
    stream = _FakeStream(None)

    with pytest.raises(grpclib.GRPCError) as exc_info:
        await handler.__mapping__()[route].func(stream)  # type: ignore[reportUnknownMemberType] -- grpclib.const.Handler.func has no typed stub
    assert exc_info.value.status == Status.INVALID_ARGUMENT


async def test_manager_primop_service_rpc_call_rejects_missing_message() -> None:
    handler = ManagerPrimopServiceHandler()
    route = "/nix.manager.ManagerPrimopService/Call"
    stream = _FakeStream(None)

    with pytest.raises(grpclib.GRPCError) as exc_info:
        await handler.__mapping__()[route].func(stream)  # type: ignore[reportUnknownMemberType] -- grpclib.const.Handler.func has no typed stub
    assert exc_info.value.status == Status.INVALID_ARGUMENT


async def test_manager_primop_service_handler_call_not_found() -> None:
    handler = ManagerPrimopServiceHandler()
    with pytest.raises(grpclib.GRPCError) as exc_info:
        await handler.call(CallPrimopRequest(name="missing"))
    assert exc_info.value.status == Status.NOT_FOUND


async def test_manager_primop_service_handler_call_wraps_exceptions() -> None:
    def _boom(*_args: object) -> None:
        raise ValueError("kaboom")

    handler = ManagerPrimopServiceHandler()
    handler.register("boom", _boom)

    with pytest.raises(grpclib.GRPCError) as exc_info:
        await handler.call(CallPrimopRequest(name="boom"))
    assert exc_info.value.status == Status.INTERNAL
    assert "kaboom" in str(exc_info.value)


async def test_manager_primop_service_handler_register_all_and_awaits_coroutine() -> None:
    """register_all bulk-registers callables; call() also awaits coroutine results."""

    async def _async_add(a: int, b: int) -> int:
        return a + b

    handler = ManagerPrimopServiceHandler()
    handler.register_all({"add": _async_add})

    response = await handler.call(
        CallPrimopRequest(
            name="add",
            args=[DeepValue(scalar=ScalarValue(int_value=2)), DeepValue(scalar=ScalarValue(int_value=3))],
        )
    )

    assert response.value is not None
    assert response.value.scalar is not None
    assert response.value.scalar.int_value == 5
