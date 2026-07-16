"""Public Store facade and its private generated-RPC transport."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, cast

from nanopynix_proto.nix.common import PathInfo
from nanopynix_proto.nix.store import (
    ComputeFsClosureRequest,
    FollowLinksToStorePathRequest,
    GetBuildLogRequest,
    GetStoreDirRequest,
    GetUriRequest,
    IsValidPathRequest,
    ParseStorePathRequest,
    QueryAllValidPathsRequest,
    QueryDerivationOutputsRequest,
    QueryPathFromHashPartRequest,
    QueryPathInfoRequest,
    QueryReferrersRequest,
    QuerySubstitutablePathsRequest,
    QueryValidDeriversRequest,
    StoreServiceBase,
)
from nanopynix_proto.nix.worker import CloseStoreRequest, OpenStoreRequest

from nanopynix._pool import _RPC_TIMEOUT as _RPC_TIMEOUT  # type: ignore[reportPrivateUsage] -- cross-class access
from nanopynix._pool import (  # type: ignore[reportPrivateUsage] -- cross-module internal utility
    WorkerBusyError,
    _grpc_call,  # type: ignore[reportPrivateUsage] -- cross-module internal utility
)
from nanopynix._rpc_proxy import RpcProxyMixin
from nanopynix.models import StorePath

if TYPE_CHECKING:
    from betterproto2 import Message

    from nanopynix._pool import (
        _WorkerManager,  # type: ignore[reportPrivateUsage] -- TYPE_CHECKING import of lifecycle type
    )


class StoreHandle(RpcProxyMixin, StoreServiceBase, rpc_service_base=StoreServiceBase):
    """Private session-bound proxy for the generated ``StoreService`` API.

    Public callers use :class:`Store`.  It exposes this transport explicitly as
    :attr:`Store.rpc` for operations which do not yet have an ergonomic method.
    """

    __slots__ = ("_active", "_pool", "_rpc_timeout", "_session_id", "_store_handle", "_uri")

    def __init__(
        self,
        pool: _WorkerManager,
        uri: str,
        session_id: str,
        rpc_timeout: float = _RPC_TIMEOUT,
    ) -> None:
        self._pool = pool
        self._rpc_timeout = rpc_timeout
        self._uri = uri
        self._session_id = session_id
        self._store_handle: int = 0
        self._active = False

    async def open(self) -> None:
        """Open a store on the worker and activate the handle."""
        resp = await self._pool.call(
            self._pool._worker_stub.open_store(  # type: ignore[reportPrivateUsage] -- cross-class access
                OpenStoreRequest(uri=self._uri), timeout=self._rpc_timeout
            )
        )
        self._store_handle = resp.store_handle
        self._active = True

    async def close(self) -> None:
        """Close the store on the worker and deactivate the handle."""
        with contextlib.suppress(Exception):
            await self._pool.call(
                self._pool._worker_stub.close_store(  # type: ignore[reportPrivateUsage] -- cross-class access
                    CloseStoreRequest(store_handle=self._store_handle),
                    timeout=self._rpc_timeout,
                )
            )
        self._active = False
        self._store_handle = 0

    async def __aenter__(self) -> StoreHandle:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    def _check_active(self) -> None:
        if not self._active:
            raise RuntimeError("StoreHandle is closed — use 'async with session.store() as store:'")

    @property
    def store_handle(self) -> int:
        """Worker-side handle for this opened store."""
        self._check_active()
        return self._store_handle

    async def _store_call(self, coro: Any) -> Any:
        """Acquire the worker lock, execute a gRPC call, and handle errors."""
        wrapped = _grpc_call(coro)  # type: ignore[reportUnknownVariableType] -- _grpc_call return type not resolved
        try:
            return await self._pool.call(wrapped)
        except WorkerBusyError:
            coro.close()
            raise

    async def _rpc_proxy_call(self, method_name: str, message: Message) -> Any:
        self._check_active()
        if self._store_handle:
            message_any = cast("Any", message)
            message_any.store_handle = self._store_handle
        method = getattr(self._pool._store_stub, method_name)  # type: ignore[reportPrivateUsage] -- cross-class access
        return await self._store_call(method(message, timeout=self._rpc_timeout))


class Store:
    """Ergonomic asynchronous facade for one opened Nix store.

    The complete generated request/response API remains available through
    :attr:`rpc`; dedicated methods use ordinary Python values and unwrap simple
    response messages.
    """

    __slots__ = ("_rpc",)

    def __init__(self, rpc: StoreHandle) -> None:
        self._rpc = rpc

    @property
    def rpc(self) -> StoreHandle:
        """Low-level generated request/response StoreService proxy."""
        return self._rpc

    @property
    def _session_id(self) -> str:
        return self._rpc._session_id  # type: ignore[reportPrivateUsage] -- facade exposes transport ownership internally

    @property
    def store_handle(self) -> int:
        """Worker-side handle for internal session integration."""
        return self._rpc.store_handle

    async def open(self) -> None:
        """Open the underlying store."""
        await self._rpc.open()

    async def close(self) -> None:
        """Close the underlying store."""
        await self._rpc.close()

    async def __aenter__(self) -> Store:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def uri(self) -> str:
        """Return the canonical URI of this store."""
        return (await self.rpc.get_uri(GetUriRequest())).uri

    async def store_dir(self) -> str:
        """Return this store's logical store directory."""
        return (await self.rpc.get_store_dir(GetStoreDirRequest())).dir

    async def parse_store_path(self, path: str) -> StorePath:
        """Validate and normalise ``path`` as a Nix store path."""
        response = await self.rpc.parse_store_path(ParseStorePathRequest(path=path))
        return StorePath(response.path)

    async def is_valid_path(self, path: str | StorePath) -> bool:
        """Return whether ``path`` is valid in this store."""
        return (await self.rpc.is_valid_path(IsValidPathRequest(path=str(path)))).valid

    async def query_path_info(self, path: str | StorePath) -> PathInfo:
        """Return metadata for a valid store path."""
        return await self.rpc.query_path_info(QueryPathInfoRequest(path=str(path)))

    async def query_all_valid_paths(self) -> list[StorePath]:
        """Return every valid path registered in this store."""
        response = await self.rpc.query_all_valid_paths(QueryAllValidPathsRequest())
        return [StorePath(path) for path in response.paths]

    async def compute_fs_closure(
        self,
        path: str | StorePath,
        *,
        flip_direction: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[StorePath]:
        """Return the filesystem closure of ``path``."""
        response = await self.rpc.compute_fs_closure(
            ComputeFsClosureRequest(
                path=str(path),
                flip_direction=flip_direction,
                include_outputs=include_outputs,
                include_derivers=include_derivers,
            )
        )
        return [StorePath(item) for item in response.paths]

    async def query_derivation_outputs(self, path: str | StorePath) -> list[StorePath]:
        """Return output paths declared by a derivation."""
        response = await self.rpc.query_derivation_outputs(QueryDerivationOutputsRequest(path=str(path)))
        return [StorePath(item) for item in response.paths]

    async def query_valid_derivers(self, path: str | StorePath) -> list[StorePath]:
        """Return valid derivations that produced ``path``."""
        response = await self.rpc.query_valid_derivers(QueryValidDeriversRequest(path=str(path)))
        return [StorePath(item) for item in response.paths]

    async def query_referrers(self, path: str | StorePath) -> list[StorePath]:
        """Return valid store paths that reference ``path``."""
        response = await self.rpc.query_referrers(QueryReferrersRequest(path=str(path)))
        return [StorePath(item) for item in response.paths]

    async def follow_links_to_store_path(self, path: str) -> StorePath:
        response = await self.rpc.follow_links_to_store_path(FollowLinksToStorePathRequest(path=path))
        return StorePath(response.path)

    async def query_path_from_hash_part(self, hash_part: str) -> StorePath | None:
        response = await self.rpc.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part=hash_part))
        return StorePath(response.path) if response.path is not None else None

    async def query_substitutable_paths(self, paths: list[str | StorePath]) -> list[StorePath]:
        response = await self.rpc.query_substitutable_paths(QuerySubstitutablePathsRequest(paths=[str(path) for path in paths]))
        return [StorePath(item) for item in response.paths]

    async def get_build_log(self, path: str | StorePath) -> str | None:
        response = await self.rpc.get_build_log(GetBuildLogRequest(path=str(path)))
        return response.log
