"""Typed helpers for RPC results."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from nanopynix.models import LogEvent

T = TypeVar("T")


class _ManagerCaller(Protocol):
    async def call(
        self,
        module: str,
        fn: str,
        args: list,
        *,
        timeout: float | None = None,
    ) -> Any: ...


class _ReservedCaller(Protocol):
    async def send_recv(
        self,
        module: str,
        fn: str,
        args: list,
        timeout: float | None = None,
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


def adapt_result(result: Any, adapter: Callable[[Any], T]) -> T:
    return adapter(result)


async def manager_call(
    caller: _ManagerCaller,
    module: str,
    fn: str,
    args: list,
    adapter: Callable[[Any], T],
    *,
    timeout: float | None = None,
) -> T:
    if timeout is None:
        result = await caller.call(module, fn, args)
    else:
        result = await caller.call(module, fn, args, timeout=timeout)
    return adapt_result(result, adapter)


async def reserved_call(
    caller: _ReservedCaller,
    module: str,
    fn: str,
    args: list,
    adapter: Callable[[Any], T],
    *,
    timeout: float | None = None,
) -> T:
    result = await caller.send_recv(module, fn, args, timeout=timeout)
    return adapt_result(result, adapter)
