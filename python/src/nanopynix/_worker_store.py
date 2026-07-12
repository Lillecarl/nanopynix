"""gRPC StoreService handler for the worker subprocess."""

from __future__ import annotations

from typing import Any

from betterproto2 import Casing
from nanopynix_proto.nix.store import StoreServiceBase

import nanopynix_store
from nanopynix._grpc_util import wrap_service_handlers
from nanopynix._service_adapter import GeneratedServiceAdapterMixin


def _store_binding_method_names() -> set[str]:
    return {
        name
        for name in dir(nanopynix_store.Store)
        if name.startswith("store_")
    }


@wrap_service_handlers
class StoreServiceHandler(
    GeneratedServiceAdapterMixin,
    StoreServiceBase,
    rpc_service_base=StoreServiceBase,
    binding_method_names=_store_binding_method_names(),
    method_prefix="store_",
):
    """gRPC handler backed by proto-shaped nanobind store methods."""

    def __init__(self, state: Any) -> None:
        self._state = state

    @property
    def _store(self) -> Any:
        if self._state.store is None:
            raise RuntimeError("store not initialized")
        return self._state.store

    def _nanobind_rpc_call(self, binding_method_name: str, message: Any) -> Any:
        request = message.to_dict(casing=Casing.SNAKE, include_default_values=True)
        if hasattr(self._store, binding_method_name):
            method = getattr(self._store, binding_method_name)
            return method(request)
        raise RuntimeError(f"missing checked nanobind store method: {binding_method_name}")
