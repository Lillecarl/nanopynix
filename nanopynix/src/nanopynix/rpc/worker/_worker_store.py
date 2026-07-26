"""gRPC StoreService handler for the worker subprocess."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanopynix_bindings import store as nanopynix_store
from nanopynix_proto.nix.store import (
    CopyClosureRequest,
    CopyClosureResponse,
    IsValidPathRequest,
    IsValidPathResponse,
    StoreServiceBase,
)

from nanopynix._wire import HandleKind
from nanopynix.rpc.worker._grpc_util import wrap_service_handlers
from nanopynix.rpc.worker._service_adapter import (
    GeneratedServiceAdapterMixin,
    HandleArgSpec,
    proto_request_to_dict,
)

if TYPE_CHECKING:
    from nanopynix._core._local import LocalStore


def _store_binding_method_names() -> set[str]:
    return {name for name in dir(nanopynix_store.Store) if name.startswith("store_")}


@wrap_service_handlers
class StoreServiceHandler(
    GeneratedServiceAdapterMixin,
    StoreServiceBase,
    rpc_service_base=StoreServiceBase,
    binding_method_names=_store_binding_method_names(),
    method_prefix="store_",
    nix_executor_attr="_state.store_limiter",
    extra_handle_args={
        # store_copy_closure's entry is gone: copy_closure has a typed handler
        # below and no longer goes through the generated dict forwarder.
        "store_build_paths_with_results": (HandleArgSpec("eval_store_handle", "eval_store", HandleKind.STORE),),
    },
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

    async def is_valid_path(self, message: IsValidPathRequest) -> IsValidPathResponse:
        return await self._run(message, self._do_is_valid_path)

    def _do_is_valid_path(self, message: IsValidPathRequest) -> IsValidPathResponse:
        return IsValidPathResponse(valid=self._resolve(message.store_handle).is_valid_path(message.path))

    async def copy_closure(self, message: CopyClosureRequest) -> CopyClosureResponse:
        return await self._run(message, self._do_copy_closure)

    def _do_copy_closure(self, message: CopyClosureRequest) -> CopyClosureResponse:
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
        store_handle = request.pop("store_handle", 0)
        store = self._get_store(store_handle)
        if hasattr(store, binding_method_name):
            method = getattr(store, binding_method_name)
            # GeneratedServiceAdapterMixin's extension-point helper is meant to be
            # called from subclass overrides of _nanobind_rpc_call defined in other
            # files -- same intentional cross-file mixin pattern _service_adapter.py's
            # own header comment already documents for _nanobind_rpc_call itself.
            extra_kwargs = self._resolve_extra_binding_args(  # pyright: ignore[reportPrivateUsage]
                binding_method_name,
                request,
            )
            return method(request, **extra_kwargs)
        raise RuntimeError(f"missing checked nanobind store method: {binding_method_name}")
