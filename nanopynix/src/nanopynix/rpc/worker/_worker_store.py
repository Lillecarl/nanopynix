"""gRPC StoreService handler for the worker subprocess."""

from __future__ import annotations

from typing import Any

from betterproto2 import Casing, OutputFormat
from nanopynix_bindings import store as nanopynix_store
from nanopynix_proto.nix.store import StoreServiceBase

from nanopynix._wire import HandleKind
from nanopynix.rpc.worker._grpc_util import wrap_service_handlers
from nanopynix.rpc.worker._service_adapter import GeneratedServiceAdapterMixin, HandleArgSpec


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
        "store_copy_closure": (HandleArgSpec("dest_store_handle", "dest_store", HandleKind.STORE, required=True),),
        "store_build_paths_with_results": (HandleArgSpec("eval_store_handle", "eval_store", HandleKind.STORE),),
        "store_build_for_humans": (HandleArgSpec("eval_store_handle", "eval_store", HandleKind.STORE),),
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

    def _nanobind_rpc_call(self, binding_method_name: str, message: Any) -> Any:
        request = message.to_dict(casing=Casing.SNAKE, include_default_values=True, output_format=OutputFormat.PYTHON)
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
