"""gRPC StoreService handler for the worker subprocess."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, Concatenate, Protocol

from nanopynix_bindings import store as nanopynix_store
from nanopynix_proto.nix.common import BuildResultList, MissingInfo
from nanopynix_proto.nix.store import (
    BuildPathsWithResultsRequest,
    CopyClosureRequest,
    CopyClosureResponse,
    IsValidPathRequest,
    IsValidPathResponse,
    QueryMissingRequest,
    StoreServiceBase,
)

from nanopynix._wire import HandleKind
from nanopynix.rpc.worker._grpc_util import wrap_service_handlers
from nanopynix.rpc.worker._service_adapter import (
    GeneratedServiceAdapterMixin,
    proto_request_to_dict,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import CoroutineType

    from nanopynix._core._local import LocalStore


def _store_binding_method_names() -> set[str]:
    return {name for name in dir(nanopynix_store.Store) if name.startswith("store_")}


class _StoreDispatch(Protocol):
    """What :func:`_store_op` needs from the handler it decorates."""

    async def _run(self, message: Any, operation: Any) -> Any: ...


def _store_op[S: _StoreDispatch, **P, R](
    handler: Callable[Concatenate[S, P], R],
) -> Callable[Concatenate[S, P], CoroutineType[Any, Any, R]]:
    """Turn a synchronous store handler into its async gRPC entry point.

    Store work runs on the worker's bounded thread pool via
    ``anyio.to_thread.run_sync``, which takes a *sync* callable -- so each RPC
    would otherwise need two methods, an ``async def`` that dispatches and a
    ``_do_`` that does the work (the shape ``_worker_eval.py`` still uses).
    This collapses the pair: write the sync body, get the dispatch for free.

    ``Concatenate``/``ParamSpec`` rather than ``Callable[[S, M], R]`` because
    the latter erases the parameter *name*, and the generated
    ``StoreServiceBase`` declares its ``message`` argument keyword-capable --
    an override that lost the name would not be a valid override.
    """

    @functools.wraps(handler)
    async def entrypoint(self: S, *args: P.args, **kwargs: P.kwargs) -> R:
        message = args[0] if args else next(iter(kwargs.values()))
        # pyright: ignore is on the next line rather than a rename because
        # _run must stay private: wrap_service_handlers treats every public
        # async method on the handler as an RPC entry point.
        return await self._run(  # type: ignore[reportPrivateUsage] -- _StoreDispatch declares _run as this protocol's whole contract
            message,
            functools.partial(handler, self),
        )

    return entrypoint


@wrap_service_handlers
class StoreServiceHandler(
    GeneratedServiceAdapterMixin,
    StoreServiceBase,
    rpc_service_base=StoreServiceBase,
    binding_method_names=_store_binding_method_names(),
    method_prefix="store_",
    nix_executor_attr="_state.store_limiter",
    # extra_handle_args is gone: its only two entries were copy_closure and
    # build_paths_with_results, the two RPCs that pass a second live Store.
    # Both now have typed handlers below, which resolve that handle directly
    # instead of declaring it for the generic forwarder to inject.
):
    """gRPC handler backed by proto-shaped nanobind store methods.

    Store operations dispatch to the worker's bounded Store executor, keeping
    the event loop free while allowing independent Store calls to overlap.
    """

    def __init__(self, state: Any) -> None:
        self._state = state

    def _get_store(self, store_handle: int) -> Any:
        return self._state.handles.get_typed(store_handle, HandleKind.STORE)

    def _resolve(self, store_handle: int) -> LocalStore:
        store: LocalStore = self._state.handles.get_typed(store_handle, HandleKind.STORE)
        return store

    async def _run(self, message: Any, operation: Any) -> Any:
        """Dispatch one store operation onto the worker's bounded Store pool.

        ``limiter``, not ``executor``: store work is not evaluator-affine, so
        independent calls overlap instead of serialising on one Nix thread.
        """
        limiter = self._state.store_limiter
        if limiter is None:
            raise RuntimeError("worker store limiter is unavailable")
        return await self._state.run_request(
            request_id=message.request_id,
            limiter=limiter,
            operation=operation,
            args=(message,),
        )

    # --- Typed handlers -------------------------------------------------
    # These replace the generated dict forwarders one RPC at a time; see
    # _install_generated_service_methods, which skips any name defined here.
    # Each is written synchronously and made async by @_store_op.

    @_store_op
    def is_valid_path(self, message: IsValidPathRequest) -> IsValidPathResponse:
        return IsValidPathResponse(valid=self._resolve(message.store_handle).is_valid_path(message.path))

    @_store_op
    def query_missing(self, message: QueryMissingRequest) -> MissingInfo:
        return self._resolve(message.store_handle).query_missing(list(message.derived_paths))

    @_store_op
    def build_paths_with_results(self, message: BuildPathsWithResultsRequest) -> BuildResultList:
        return BuildResultList(
            results=self._resolve(message.store_handle).build_paths_with_results(
                list(message.derived_paths),
                build_mode=message.build_mode,
                eval_store=self._resolve(message.eval_store_handle) if message.eval_store_handle else None,
            ),
        )

    @_store_op
    def copy_closure(self, message: CopyClosureRequest) -> CopyClosureResponse:
        if not message.dest_store_handle:
            # A missing argument, not a store failure: a copy with no
            # destination is meaningless, and the proto documents the field as
            # required.
            raise ValueError("dest_store_handle is required for copy_closure")
        self._resolve(message.store_handle).copy_closure(
            list(message.paths),
            self._resolve(message.dest_store_handle),
            repair=message.repair,
            check_sigs=message.check_sigs,
            substitute=message.substitute,
        )
        return CopyClosureResponse()

    def _nanobind_rpc_call(self, binding_method_name: str, message: Any) -> Any:
        request = proto_request_to_dict(message)
        store = self._get_store(request.pop("store_handle", 0))
        if hasattr(store, binding_method_name):
            return getattr(store, binding_method_name)(request)
        raise RuntimeError(f"missing checked nanobind store method: {binding_method_name}")
