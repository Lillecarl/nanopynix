"""Helpers for local proxy classes that implement generated RPC service APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from betterproto2 import Message


class RpcProxyMixin:
    """Mixin for installing generated service methods onto local proxy facades."""

    def __init_subclass__(
        cls,
        *,
        rpc_service_base: type | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if rpc_service_base is not None:
            cls._install_rpc_forwarders(rpc_service_base)

    @classmethod
    def _install_rpc_forwarders(cls, service_base: type) -> None:
        for method_name, method in service_base.__dict__.items():
            if method_name.startswith("_") or not callable(method):
                continue
            setattr(cls, method_name, _make_forwarder(method_name))

    async def _rpc_proxy_call(self, method_name: str, message: Message) -> Any:
        raise NotImplementedError


def _make_forwarder(method_name: str) -> Callable[[RpcProxyMixin, Message], Coroutine[Any, Any, Any]]:
    async def _forward(self: RpcProxyMixin, message: Message) -> Any:
        return await self._rpc_proxy_call(method_name, message)

    _forward.__name__ = method_name
    return _forward
