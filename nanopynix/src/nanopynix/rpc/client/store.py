"""Public Store facade and its private generated-RPC transport."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from nanopynix_proto.nix.store import (
    AddIndirectRootRequest,
    AddPermRootRequest,
    AddTempRootRequest,
    AddToStoreRequest,
    BuildPathsWithResultsRequest,
    CollectGarbageRequest,
    ComputeFsClosureRequest,
    ComputeStorePathRequest,
    EnsurePathRequest,
    FindRootsRequest,
    FollowLinksToStorePathRequest,
    GcAction,
    GcRoot,
    GetBuildLogRequest,
    GetStoreDirRequest,
    GetStoreDirsRequest,
    GetUriRequest,
    IsValidPathRequest,
    OptimiseStoreRequest,
    ParseStorePathRequest,
    QueryAllValidPathsRequest,
    QueryDerivationOutputsRequest,
    QueryMissingRequest,
    QueryPathFromHashPartRequest,
    QueryPathInfoRequest,
    QueryReferrersRequest,
    QuerySubstitutablePathsRequest,
    QueryValidDeriversRequest,
    ReadDerivationRequest,
    StoreDirs,
    StoreServiceBase,
    VerifyStoreRequest,
)
from nanopynix_proto.nix.worker import CloseStoreRequest, OpenStoreRequest

from nanopynix.models import BuildResult, Derivation, GcResult, MissingInfo, StorePath
from nanopynix.rpc.client._pool import (
    _RPC_TIMEOUT as _RPC_TIMEOUT,  # type: ignore[reportPrivateUsage] -- cross-class access
)
from nanopynix.rpc.client._pool import WorkerDiedError
from nanopynix.rpc.client._rpc_proxy import RpcProxyMixin

if TYPE_CHECKING:
    from betterproto2 import Message
    from nanopynix_proto.nix.common import PathInfo

    from nanopynix.rpc.client._pool import (
        _WorkerClient,  # type: ignore[reportPrivateUsage] -- TYPE_CHECKING import of lifecycle type
    )


class StoreHandle(RpcProxyMixin, StoreServiceBase, rpc_service_base=StoreServiceBase):
    """Private session-bound proxy for the generated ``StoreService`` API.

    Public callers use :class:`Store`.  It exposes this transport explicitly as
    :attr:`Store.rpc` for operations which do not yet have an ergonomic method.
    """

    __slots__ = ("_active", "_dependent_evals", "_pool", "_rpc_timeout", "_session_id", "_store_handle", "_uri")

    def __init__(
        self,
        pool: _WorkerClient,
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
        self._dependent_evals: set[Any] = set()

    async def open(self) -> None:
        """Open a store on the worker and activate the handle."""
        resp = await self._pool.call(
            self._pool._worker_stub.open_store(  # type: ignore[reportPrivateUsage] -- cross-class access
                OpenStoreRequest(uri=self._uri), timeout=self._rpc_timeout
            )
        )
        self._store_handle = resp.store_handle
        self._active = True

    async def close(self, *, force: bool = False) -> None:
        """Close the store, optionally closing its dependent evaluators first."""
        if not self._active:
            return
        if force:
            for dependent_eval in tuple(self._dependent_evals):
                await dependent_eval.close()
        try:
            await self._pool.call(
                self._pool._worker_stub.close_store(  # type: ignore[reportPrivateUsage] -- cross-class access
                    CloseStoreRequest(store_handle=self._store_handle, force=force),
                    timeout=self._rpc_timeout,
                )
            )
        except WorkerDiedError:
            # The remote handle disappeared with its worker, so no close RPC
            # remains possible or necessary.
            pass
        finally:
            self._active = False
            self._store_handle = 0

    def _register_dependent_eval(self, eval_session: Any) -> None:
        self._dependent_evals.add(eval_session)

    def _unregister_dependent_eval(self, eval_session: Any) -> None:
        self._dependent_evals.discard(eval_session)

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
        return await self._pool.call(coro)

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

    async def close(self, *, force: bool = False) -> None:
        """Close the underlying store, optionally closing its evaluator first."""
        await self._rpc.close(force=force)

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
        """Resolve a path that may traverse symlinks to its containing store path."""
        response = await self.rpc.follow_links_to_store_path(FollowLinksToStorePathRequest(path=path))
        return StorePath(response.path)

    async def query_path_from_hash_part(self, hash_part: str) -> StorePath | None:
        """Return the valid store path whose hash component is ``hash_part``, if any."""
        response = await self.rpc.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part=hash_part))
        return StorePath(response.path) if response.path is not None else None

    async def query_substitutable_paths(self, paths: list[str | StorePath]) -> list[StorePath]:
        """Return the subset of ``paths`` that can be substituted from a binary cache."""
        response = await self.rpc.query_substitutable_paths(QuerySubstitutablePathsRequest(paths=[str(path) for path in paths]))
        return [StorePath(item) for item in response.paths]

    async def get_build_log(self, path: str | StorePath) -> str | None:
        """Return the build log for ``path``, or ``None`` if no log is available."""
        response = await self.rpc.get_build_log(GetBuildLogRequest(path=str(path)))
        return response.log

    async def query_missing(
        self,
        derived_paths: list[str | StorePath],
        /,
    ) -> MissingInfo:
        """Return which of ``derived_paths`` still need to be built or substituted."""
        return await self.rpc.query_missing(
            QueryMissingRequest(derived_paths=[str(p) for p in derived_paths])
        )

    async def build_paths_with_results(
        self,
        derived_paths: list[str | StorePath],
        /,
        *,
        build_mode: int = 0,
        eval_store: Store | None = None,
    ) -> list[BuildResult]:
        """Build derived paths and return Nix's result for each path.

        A plain derivation path builds all outputs. Use Nix's ``^`` syntax to
        select explicit outputs in a canonical DerivedPath string.
        """
        if eval_store is not None and eval_store._session_id != self._session_id:
            raise ValueError("eval_store belongs to a different Session")
        response = await self.rpc.build_paths_with_results(
            BuildPathsWithResultsRequest(
                derived_paths=[str(path) for path in derived_paths],
                build_mode=build_mode,
                eval_store_handle=0 if eval_store is None else eval_store.store_handle,
            )
        )
        return list(response.results)

    async def read_derivation(self, drv_path: str | StorePath, /) -> Derivation:
        """Parse and return the ``.drv`` file at ``drv_path``."""
        return await self.rpc.read_derivation(ReadDerivationRequest(path=str(drv_path)))

    async def collect_garbage(
        self,
        action: GcAction,
        /,
        *,
        ignore_liveness: bool = False,
        paths_to_delete: list[str | StorePath] | tuple[()] = (),
        max_freed: int = 2**64 - 1,
    ) -> GcResult:
        """Run a garbage-collection pass.

        Args:
            action: What the collector should do — e.g. list or delete dead
                paths (see :class:`~nanopynix_proto.nix.store.GcAction`).
            ignore_liveness: Delete ``paths_to_delete`` even if reachable
                from a root.
            paths_to_delete: Restrict the action to these paths, if given.
            max_freed: Stop once this many bytes have been freed.

        Returns:
            The paths affected and total bytes freed.
        """
        response = await self.rpc.collect_garbage(
            CollectGarbageRequest(
                action=action,
                ignore_liveness=ignore_liveness,
                paths_to_delete=[str(p) for p in paths_to_delete],
                max_freed=max_freed,
            )
        )
        return GcResult(
            paths=[StorePath(p) for p in response.paths],
            bytes_freed=response.bytes_freed,
        )

    async def find_roots(self, *, censor: bool = False) -> list[GcRoot]:
        """Return the garbage collector roots."""
        response = await self.rpc.find_roots(FindRootsRequest(censor=censor))
        return list(response.roots)

    async def store_dirs(self) -> StoreDirs:
        """Return this store's full set of configured directories."""
        return await self.rpc.get_store_dirs(GetStoreDirsRequest())

    async def add_temp_root(self, path: str | StorePath, /) -> None:
        """Add a temporary GC root for this store session."""
        await self.rpc.add_temp_root(AddTempRootRequest(path=str(path)))

    async def add_perm_root(self, path: str | StorePath, gc_root: str, /) -> str:
        """Add a permanent GC root symlink and return its resolved path."""
        response = await self.rpc.add_perm_root(AddPermRootRequest(store_path=str(path), gc_root=gc_root))
        return response.path

    async def add_indirect_root(self, path: str, /) -> None:
        """Register an indirect GC root."""
        await self.rpc.add_indirect_root(AddIndirectRootRequest(path=path))

    async def ensure_path(self, path: str | StorePath, /) -> None:
        """Ensure a store path is valid, substituting it if available."""
        await self.rpc.ensure_path(EnsurePathRequest(path=str(path)))

    async def optimise_store(self) -> None:
        """Optimise store disk usage by hard-linking duplicate files."""
        await self.rpc.optimise_store(OptimiseStoreRequest())

    async def verify_store(self, *, check_contents: bool = False, repair: bool = False) -> bool:
        """Verify store integrity, returning whether errors were found."""
        response = await self.rpc.verify_store(VerifyStoreRequest(check_contents=check_contents, repair=repair))
        return response.errors

    async def compute_store_path(
        self,
        path: str,
        *,
        name: str | None = None,
        method: str = "nar",
        hash_algo: str = "sha256",
    ) -> StorePath:
        """Compute the store path content-addressing ``path`` without adding it."""
        response = await self.rpc.compute_store_path(
            ComputeStorePathRequest(path=path, name=name, method=method, hash_algo=hash_algo)
        )
        return StorePath(response.path)

    async def add_to_store(
        self,
        path: str,
        *,
        name: str | None = None,
        method: str = "nar",
        hash_algo: str = "sha256",
    ) -> StorePath:
        """Add a file or directory to this store."""
        response = await self.rpc.add_to_store(
            AddToStoreRequest(path=path, name=name, method=method, hash_algo=hash_algo)
        )
        return StorePath(response.path)
