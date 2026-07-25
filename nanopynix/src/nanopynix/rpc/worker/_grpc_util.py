"""Shared gRPC helpers for worker service handlers."""

from __future__ import annotations

import functools
import inspect
from typing import TYPE_CHECKING, Any

from grpclib.const import Status
from grpclib.exceptions import GRPCError

from nanopynix.rpc._status_details import details_for_exception

if TYPE_CHECKING:
    from collections.abc import Callable


def convert_handler_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: convert unhandled exceptions to GRPCError with the original message.

    grpclib's server catches unhandled exceptions and sends a generic
    ``Status.UNKNOWN / "Internal Server Error"``.  This decorator preserves
    the original error message so the client can classify it via
    ``exceptions.from_response()``.

    A :class:`~nanopynix.NixError`'s ``raw``/``info`` -- the ``nix::ErrorInfo``
    the bindings recovered from C++ -- go into ``GRPCError.details`` as a
    ``nix.common.NixErrorInfo``, which rides the ``grpc-status-details-bin``
    trailer inside a standard ``google.rpc.Status``. That only reaches the
    client if both ends installed the codec; see
    :mod:`nanopynix.rpc._status_details`.
    """

    @functools.wraps(func)
    async def wrapper(self: Any, message: Any) -> Any:
        try:
            return await func(self, message)
        except GRPCError:
            raise
        except Exception as exc:
            details = details_for_exception(exc)
            raise GRPCError(Status.UNKNOWN, f"{type(exc).__name__}: {exc}", details) from exc

    return wrapper


def wrap_service_handlers(cls: type) -> type:
    """Class decorator: apply ``convert_handler_errors`` to all async handler methods."""
    for name in list(cls.__dict__):
        if name.startswith("_"):
            continue
        method = cls.__dict__[name]
        if inspect.iscoroutinefunction(method):
            setattr(cls, name, convert_handler_errors(method))
    return cls
