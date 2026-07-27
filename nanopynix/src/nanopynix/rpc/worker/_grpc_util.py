"""Shared gRPC helpers for worker service handlers."""

from __future__ import annotations

import functools
import inspect
from typing import TYPE_CHECKING, Any, Concatenate, Protocol

from grpclib.const import Status
from grpclib.exceptions import GRPCError

from nanopynix._typechecking import BEARTYPING
from nanopynix.rpc._status_details import details_for_exception

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable
    from types import CoroutineType


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


class _WorkerDispatch(Protocol):
    """What :func:`worker_op` needs from the handler it decorates.

    Deliberately just ``_run``. *Where* that runs the operation is the
    handler's own business, and the two differ: the store's sends it to a
    bounded pool so independent calls overlap, the evaluator's to the one Nix
    thread that evaluator is confined to. Neither difference is visible from
    here, which is what lets one decorator serve both.
    """

    async def _run(self, message: Any, operation: Any) -> Any: ...


def worker_op[S: _WorkerDispatch, **P, R](
    handler: Callable[Concatenate[S, P], R],
) -> Callable[Concatenate[S, P], CoroutineType[Any, Any, R]]:
    """Turn a synchronous handler body into its async gRPC entry point.

    Nix work runs on a thread -- ``anyio.to_thread.run_sync`` for store calls,
    a confined evaluator thread for eval -- and both want a *sync* callable. So
    each RPC would otherwise need two methods, an ``async def`` that dispatches
    and a ``_do_`` that does the work. This collapses the pair: write the sync
    body under the RPC's own name, get the dispatch for free.

    ``Concatenate``/``ParamSpec`` rather than ``Callable[[S, M], R]`` because
    the latter erases the parameter *name*, and the generated service bases
    declare their ``message`` argument keyword-capable -- an override that lost
    the name would not be a valid override.
    """

    @functools.wraps(handler)
    async def entrypoint(self: S, *args: P.args, **kwargs: P.kwargs) -> R:
        # "message" by name, not "the first kwarg": the service bases declare
        # the argument keyword-capable, so a caller may pass it either way, and
        # picking whichever key happens to come first would silently take the
        # wrong object the moment a second keyword exists.
        message = args[0] if args else kwargs["message"]
        # pyright: ignore is on the next line rather than a rename because
        # _run must stay private: wrap_service_handlers treats every public
        # async method on the handler as an RPC entry point.
        return await self._run(  # type: ignore[reportPrivateUsage] -- _WorkerDispatch declares _run as this protocol's whole contract
            message,
            functools.partial(handler, self),
        )

    return entrypoint


def wrap_service_handlers[C](cls: type[C]) -> type[C]:
    """Class decorator: apply ``convert_handler_errors`` to all async handler methods.

    Generic in the class it decorates rather than ``(cls: type) -> type``. The
    erased form made every decorated handler an unknown type to its callers,
    which is only invisible in production because the handlers are constructed
    by name in one place; a test that builds one and calls a method on it saw
    ``Unknown`` for both.
    """
    for name in list(cls.__dict__):
        if name.startswith("_"):
            continue
        method = cls.__dict__[name]
        if inspect.iscoroutinefunction(method):
            setattr(cls, name, convert_handler_errors(method))
    return cls
