"""Typed helpers for RPC results and optional log capture."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol, TypeVar, overload

from nanopynix.models import Capture, LogEvent

T = TypeVar("T")


class _ManagerCaller(Protocol):
    async def call(
        self,
        module: str,
        fn: str,
        args: list,
        *,
        timeout: float | None = None,
        capture: bool = False,
    ) -> Any: ...


class _ReservedCaller(Protocol):
    async def send_recv(
        self,
        module: str,
        fn: str,
        args: list,
        timeout: float | None = None,
        capture: bool = False,
    ) -> Any: ...


def raw_to_log_event(raw: dict[str, Any]) -> LogEvent:
    """Convert a raw worker log-event dict to a ``LogEvent`` model."""
    data: dict[str, Any] = {
        "request_id": raw.get("request_id", raw.get("id", 0)),
        "action": raw["action"],
        "args": raw["args"],
    }
    if raw.get("action") == "result" and len(raw.get("args", [])) > 1:
        data["result_type"] = raw["args"][1]
    return LogEvent.model_validate(data)


def identity(value: Any) -> Any:
    return value


@overload
def adapt_result(result: Any, adapter: Callable[[Any], T], *, capture: Literal[False] = False) -> T: ...


@overload
def adapt_result(result: Any, adapter: Callable[[Any], T], *, capture: Literal[True]) -> Capture[T]: ...


@overload
def adapt_result(result: Any, adapter: Callable[[Any], T], *, capture: bool) -> T | Capture[T]: ...


def adapt_result(result: Any, adapter: Callable[[Any], T], *, capture: bool = False) -> T | Capture[T]:
    if capture:
        value, raw_events = result
        return Capture(
            value=adapter(value),
            logs=[raw_to_log_event(event) for event in raw_events],
        )
    return adapter(result)


@overload
async def manager_call(
    caller: _ManagerCaller,
    module: str,
    fn: str,
    args: list,
    adapter: Callable[[Any], T],
    *,
    timeout: float | None = None,
    capture: Literal[False] = False,
) -> T: ...


@overload
async def manager_call(
    caller: _ManagerCaller,
    module: str,
    fn: str,
    args: list,
    adapter: Callable[[Any], T],
    *,
    timeout: float | None = None,
    capture: Literal[True],
) -> Capture[T]: ...


@overload
async def manager_call(
    caller: _ManagerCaller,
    module: str,
    fn: str,
    args: list,
    adapter: Callable[[Any], T],
    *,
    timeout: float | None = None,
    capture: bool,
) -> T | Capture[T]: ...


async def manager_call(
    caller: _ManagerCaller,
    module: str,
    fn: str,
    args: list,
    adapter: Callable[[Any], T],
    *,
    timeout: float | None = None,
    capture: bool = False,
) -> T | Capture[T]:
    if timeout is None:
        result = await caller.call(module, fn, args, capture=capture)
    else:
        result = await caller.call(module, fn, args, timeout=timeout, capture=capture)
    return adapt_result(result, adapter, capture=capture)


@overload
async def reserved_call(
    caller: _ReservedCaller,
    module: str,
    fn: str,
    args: list,
    adapter: Callable[[Any], T],
    *,
    timeout: float | None = None,
    capture: Literal[False] = False,
) -> T: ...


@overload
async def reserved_call(
    caller: _ReservedCaller,
    module: str,
    fn: str,
    args: list,
    adapter: Callable[[Any], T],
    *,
    timeout: float | None = None,
    capture: Literal[True],
) -> Capture[T]: ...


@overload
async def reserved_call(
    caller: _ReservedCaller,
    module: str,
    fn: str,
    args: list,
    adapter: Callable[[Any], T],
    *,
    timeout: float | None = None,
    capture: bool,
) -> T | Capture[T]: ...


async def reserved_call(
    caller: _ReservedCaller,
    module: str,
    fn: str,
    args: list,
    adapter: Callable[[Any], T],
    *,
    timeout: float | None = None,
    capture: bool = False,
) -> T | Capture[T]:
    result = await caller.send_recv(module, fn, args, timeout=timeout, capture=capture)
    return adapt_result(result, adapter, capture=capture)
