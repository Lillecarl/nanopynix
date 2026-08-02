"""Unit coverage for nanopynix.rpc.client._manager.

This module is pure Python (no daemon/store/nix dependency): small grpclib
ServiceBase classes. The existing integration coverage in
test_primop_rpc.py only exercises the success path through a real Nix
session, so it never reaches the base classes' UNIMPLEMENTED stubs or the
NOT_FOUND / exception-wrapping error paths in ManagerPrimopServiceHandler.call.
These are "dumb coverage" tests: each one pins down a single small branch
directly, without an RPC round trip.

Scalar (de)serialization coverage for the shared codec itself lives in
tests/nanopynix/core/test_codec.py, not here -- _manager.py no longer
defines its own copy.
"""

from __future__ import annotations

from typing import Any

import grpclib
import pytest
from grpclib.const import Status
from nanopynix_proto.nix.common import DeepValue, LogEvent, ScalarValue
from nanopynix_proto.nix.manager import CallPrimopRequest

from nanopynix.rpc._primop_wire import decode
from nanopynix.rpc.client._manager import (
    ManagerPrimopServiceBase,
    ManagerPrimopServiceHandler,
    ManagerServiceBase,
    ManagerServiceHandler,
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


async def test_manager_primop_service_handler_encodes_a_caller_exception() -> None:
    """The handler raises the wire form, not a ``GRPCError`` (#33).

    It used to raise ``GRPCError(Status.INTERNAL, str(exc))``. The backchannel
    frame carries one ``error: str`` field and the transport fills it with
    ``str()`` of whatever the handler raised, so the status was discarded and
    the ``GRPCError``'s repr became the message Nix showed. The assertion on
    ``Status.INTERNAL`` therefore pinned a value no caller could observe.

    What replaces it is stricter, not looser: the message must decode to the
    exact text Nix renders. See :mod:`nanopynix.rpc._primop_wire`, and
    ``tests/nanopynix/primops/test_primop_error_parity.py`` for the end-to-end
    proof that all three primop paths now read the same.

    The two tests above still expect a ``GRPCError``. Those failures come from
    the handler itself rather than from the caller's function, they are raised
    before the call is attempted, and #33 does not cover them.
    """

    def _boom(*_args: object) -> None:
        raise ValueError("kaboom")

    handler = ManagerPrimopServiceHandler()
    handler.register("boom", _boom)

    with pytest.raises(RuntimeError) as exc_info:
        await handler.call(CallPrimopRequest(name="boom"))
    assert not isinstance(exc_info.value, grpclib.GRPCError), "a GRPCError's status does not survive this transport"
    # A ValueError is a deliberate rejection, so Nix shows the message bare.
    assert decode(str(exc_info.value)) == "kaboom"


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
        ),
    )

    assert response.value is not None
    assert response.value.scalar is not None
    assert response.value.scalar.int_value == 5
