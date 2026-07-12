"""Store facade — checked proxy for the generated StoreService API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanopynix_proto.nix.store import StoreServiceBase

from nanopynix._pool import _RPC_TIMEOUT, WorkerBusyError, _grpc_call
from nanopynix._rpc_proxy import RpcProxyMixin

if TYPE_CHECKING:
    from betterproto2 import Message

    from nanopynix._pool import _WorkerManager


class StoreHandle(RpcProxyMixin, StoreServiceBase, rpc_service_base=StoreServiceBase):
    """Session-bound proxy for the generated ``StoreService`` request/response API."""

    __slots__ = ("_active", "_pool", "_session_id", "_uri")

    def __init__(self, pool: _WorkerManager, uri: str, session_id: str) -> None:
        self._pool = pool
        self._uri = uri
        self._session_id = session_id
        self._active = False

    async def open(self) -> None:
        """Activate the handle, called by context manager or manually."""
        self._active = True

    async def close(self) -> None:
        """Deactivate the handle."""
        self._active = False

    async def __aenter__(self) -> StoreHandle:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    def _check_active(self) -> None:
        if not self._active:
            raise RuntimeError("StoreHandle is closed — use 'async with session.store() as store:'")

    async def _store_call(self, coro: Any) -> Any:
        """Acquire the worker lock, execute a gRPC call, and handle errors."""
        wrapped = _grpc_call(coro)
        try:
            return await self._pool.call(wrapped)
        except WorkerBusyError:
            coro.close()
            raise

    async def _rpc_proxy_call(self, method_name: str, message: Message) -> Any:
        self._check_active()
        method = getattr(self._pool._store_stub, method_name)
        return await self._store_call(method(message, timeout=_RPC_TIMEOUT))

# Backward-compatible alias
Store = StoreHandle
