"""Store facade — checked proxy for the generated StoreService API."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from nanopynix_proto.nix.store import StoreServiceBase

from nanopynix._pool import _RPC_TIMEOUT, WorkerBusyError, _grpc_call
from nanopynix._rpc_proxy import RpcProxyMixin

if TYPE_CHECKING:
    from betterproto2 import Message

    from nanopynix._pool import _WorkerManager


class StoreHandle(RpcProxyMixin, StoreServiceBase, rpc_service_base=StoreServiceBase):
    """Session-bound proxy for the generated ``StoreService`` request/response API."""

    __slots__ = ("_active", "_pool", "_session_id", "_store_handle", "_uri")

    def __init__(self, pool: _WorkerManager, uri: str, session_id: str) -> None:
        self._pool = pool
        self._uri = uri
        self._session_id = session_id
        self._store_handle: int = 0
        self._active = False

    async def open(self) -> None:
        """Open a store on the worker and activate the handle."""
        from nanopynix_proto.nix.worker import OpenStoreRequest

        resp = await self._pool.call(
            self._pool._worker_stub.open_store(
                OpenStoreRequest(uri=self._uri), timeout=_RPC_TIMEOUT
            )
        )
        self._store_handle = resp.store_handle
        self._active = True

    async def close(self) -> None:
        """Close the store on the worker and deactivate the handle."""
        from nanopynix_proto.nix.worker import CloseStoreRequest

        with contextlib.suppress(Exception):
            await self._pool.call(
                self._pool._worker_stub.close_store(
                    CloseStoreRequest(store_handle=self._store_handle),
                    timeout=_RPC_TIMEOUT,
                )
            )
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
        if self._store_handle:
            setattr(message, "store_handle", self._store_handle)
        method = getattr(self._pool._store_stub, method_name)
        return await self._store_call(method(message, timeout=_RPC_TIMEOUT))

# Backward-compatible alias
Store = StoreHandle
