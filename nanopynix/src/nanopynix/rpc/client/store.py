"""Public Store facade and its private generated-RPC transport."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from nanopynix_bindings.store import BuildMode
from nanopynix_proto.nix.store import (
    AddIndirectRootRequest,
    AddPermRootRequest,
    AddTempRootRequest,
    AddToStoreRequest,
    BuildPathsWithResultsRequest,
    CollectGarbageRequest,
    ComputeFsClosureRequest,
    ComputeStorePathRequest,
    CopyClosureRequest,
    DumpDbRequest,
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
    WriteDevShellDerivationRequest,
)
from nanopynix_proto.nix.worker import CloseStoreRequest, OpenStoreRequest

from nanopynix._typechecking import BEARTYPING, no_runtime_type_check
from nanopynix._wire import DEFAULT_CA_METHOD, DEFAULT_HASH_ALGO, NO_GC_LIMIT
from nanopynix.exceptions import SessionClosedError, StoreClosedError, WorkerDiedError
from nanopynix.models import BuildResult, Derivation, DerivedPath, GcResult, MissingInfo, StorePath
from nanopynix.protocols import AsyncStore
from nanopynix.rpc.client._rpc_proxy import RpcProxyMixin
from nanopynix.settings import DEFAULT_RPC_TIMEOUT_SECONDS

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

    from betterproto2 import Message
    from nanopynix_proto.nix.common import PathInfo

    from nanopynix.rpc.client._pool import WorkerClient


class StoreHandle(RpcProxyMixin, StoreServiceBase, rpc_service_base=StoreServiceBase):
    """Private session-bound proxy for the generated ``StoreService`` API.

    Public callers use :class:`Store`.  It exposes this transport explicitly as
    :attr:`Store.rpc` for operations which do not yet have an ergonomic method.
    """

    __slots__ = ("_active", "_dependent_evals", "_pool", "_rpc_timeout", "_session_id", "_store_handle", "_uri")

    def __init__(
        self,
        pool: WorkerClient,
        uri: str,
        session_id: str,
        rpc_timeout: float = DEFAULT_RPC_TIMEOUT_SECONDS,
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
        resp = await self._pool.invoke(
            self._pool.worker_stub.open_store,
            OpenStoreRequest(uri=self._uri),
            timeout=self._rpc_timeout,
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
            await self._pool.invoke(
                self._pool.worker_stub.close_store,
                CloseStoreRequest(store_handle=self._store_handle, force=force),
                timeout=self._rpc_timeout,
            )
        except (WorkerDiedError, SessionClosedError):
            # The remote handle disappeared with its worker, so no close RPC
            # remains possible or necessary. SessionClosedError is the orderly
            # version of the same thing -- the session closed first, taking the
            # worker with it -- and it only became reachable here once
            # WorkerClient.invoke started reporting a closed session as such.
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

    @property
    def is_open(self) -> bool:
        """True once this store is open, and False again after it closes.

        Read by ``Session.set_settings``: a store reads its settings while Nix
        constructs it, so changing a global while one is open would be lost on
        that store without a word.
        """
        return self._active

    def _check_active(self) -> None:
        if not self._active:
            raise StoreClosedError("StoreHandle is closed — use 'async with session.store() as store:'")

    @property
    def store_handle(self) -> int:
        """Worker-side handle for this opened store."""
        self._check_active()
        return self._store_handle

    async def _rpc_proxy_call(self, method_name: str, message: Message) -> Any:
        self._check_active()
        if self._store_handle:
            message_any = cast("Any", message)
            message_any.store_handle = self._store_handle
        method = getattr(self._pool.store_stub, method_name)
        return await self._pool.invoke(method, message, timeout=self._rpc_timeout)


class Store(AsyncStore):
    """Ergonomic asynchronous facade for one opened Nix store.

    The complete generated request/response API remains available through
    :attr:`rpc`; dedicated methods use ordinary Python values and unwrap simple
    response messages.
    """

    # __weakref__ so the owning Session can hold these in a WeakSet: it closes
    # the stores it handed out, but must not be the reason one stays alive.
    __slots__ = ("__weakref__", "_rpc")

    def __init__(self, rpc: StoreHandle) -> None:
        self._rpc = rpc

    @property
    def rpc(self) -> StoreHandle:
        """Low-level generated request/response StoreService proxy."""
        return self._rpc

    @property
    def _session_id(self) -> str:
        return self._rpc._session_id  # type: ignore[reportPrivateUsage] -- facade exposes transport ownership internally  # noqa: SLF001

    @property
    def _store_handle(self) -> int:
        """Worker-side handle, for wiring this store into a remote evaluator.

        Private: it names a slot in one worker process, so it means nothing to
        a caller and nothing at all on the inproc engine.
        """
        return self._rpc.store_handle

    @property
    def is_open(self) -> bool:
        """True while this store is open. See :attr:`StoreHandle.is_open`."""
        return self._rpc.is_open

    async def open(self) -> None:
        await self._rpc.open()

    async def close(self, *, force: bool = False) -> None:
        await self._rpc.close(force=force)

    async def __aenter__(self) -> Store:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def uri(self, *, with_params: bool = False) -> str:
        """Return this store's URI, optionally including configuration parameters."""
        return (await self.rpc.get_uri(GetUriRequest(with_params=with_params))).uri

    async def store_dir(self) -> str:
        return (await self.rpc.get_store_dir(GetStoreDirRequest())).dir

    async def parse_store_path(self, path: str) -> StorePath:
        response = await self.rpc.parse_store_path(ParseStorePathRequest(path=path))
        return StorePath(response.path)

    async def is_valid_path(self, path: str | StorePath) -> bool:
        return (await self.rpc.is_valid_path(IsValidPathRequest(path=str(path)))).valid

    async def query_path_info(self, path: str | StorePath) -> PathInfo:
        return await self.rpc.query_path_info(QueryPathInfoRequest(path=str(path)))

    async def query_all_valid_paths(self) -> list[StorePath]:
        response = await self.rpc.query_all_valid_paths(QueryAllValidPathsRequest())
        return [StorePath(path) for path in response.paths]

    async def dump_db(
        self,
        paths: Sequence[str | StorePath],
        /,
        *,
        show_derivers: bool = True,
        show_hash: bool = True,
    ) -> str:
        response = await self.rpc.dump_db(
            DumpDbRequest(
                paths=[str(path) for path in paths],
                show_derivers=show_derivers,
                show_hash=show_hash,
            ),
        )
        return response.registration

    async def compute_fs_closure(
        self,
        path: str | StorePath,
        *,
        flip_direction: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[StorePath]:
        response = await self.rpc.compute_fs_closure(
            ComputeFsClosureRequest(
                path=str(path),
                flip_direction=flip_direction,
                include_outputs=include_outputs,
                include_derivers=include_derivers,
            ),
        )
        return [StorePath(item) for item in response.paths]

    async def query_derivation_outputs(self, path: str | StorePath) -> list[StorePath]:
        response = await self.rpc.query_derivation_outputs(QueryDerivationOutputsRequest(path=str(path)))
        return [StorePath(item) for item in response.paths]

    async def query_valid_derivers(self, path: str | StorePath) -> list[StorePath]:
        response = await self.rpc.query_valid_derivers(QueryValidDeriversRequest(path=str(path)))
        return [StorePath(item) for item in response.paths]

    async def query_referrers(self, path: str | StorePath) -> list[StorePath]:
        response = await self.rpc.query_referrers(QueryReferrersRequest(path=str(path)))
        return [StorePath(item) for item in response.paths]

    async def follow_links_to_store_path(self, path: str) -> StorePath:
        response = await self.rpc.follow_links_to_store_path(FollowLinksToStorePathRequest(path=path))
        return StorePath(response.path)

    async def query_path_from_hash_part(self, hash_part: str) -> StorePath | None:
        response = await self.rpc.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part=hash_part))
        return StorePath(response.path) if response.path is not None else None

    async def query_substitutable_paths(self, paths: list[str | StorePath]) -> list[StorePath]:
        response = await self.rpc.query_substitutable_paths(
            QuerySubstitutablePathsRequest(paths=[str(path) for path in paths])
        )
        return [StorePath(item) for item in response.paths]

    async def get_build_log(self, path: str | StorePath) -> str | None:
        response = await self.rpc.get_build_log(GetBuildLogRequest(path=str(path)))
        return response.log

    async def query_missing(
        self,
        derived_paths: list[str | StorePath],
        /,
    ) -> MissingInfo:
        """Return which of ``derived_paths`` still need to be built or substituted.

        A plain derivation path means all outputs -- see
        :meth:`~nanopynix.models.DerivedPath.for_build`. It is applied here,
        before the request goes on the wire, so the worker receives the same
        canonical string the inproc engine hands its own bindings.
        """
        return await self.rpc.query_missing(
            QueryMissingRequest(derived_paths=[DerivedPath(str(p)).for_build() for p in derived_paths]),
        )

    async def build_paths_with_results(
        self,
        derived_paths: list[str | StorePath],
        /,
        *,
        build_mode: int = BuildMode.Normal.value,
        eval_store: Store | None = None,
    ) -> list[BuildResult]:
        """Build derived paths and return Nix's result for each path.

        A plain derivation path builds all outputs -- see
        :meth:`~nanopynix.models.DerivedPath.for_build`, applied here before
        the request goes on the wire. Use Nix's ``^`` syntax to select
        explicit outputs in a canonical DerivedPath string.
        """
        if eval_store is not None and eval_store._session_id != self._session_id:  # noqa: SLF001 -- eval_store is another Store of this same class; comparing its private session id
            raise ValueError("eval_store belongs to a different Session")
        response = await self.rpc.build_paths_with_results(
            BuildPathsWithResultsRequest(
                derived_paths=[DerivedPath(str(path)).for_build() for path in derived_paths],
                build_mode=build_mode,
                eval_store_handle=0 if eval_store is None else eval_store._store_handle,  # noqa: SLF001 -- one Store reads another's worker handle to name the eval store
            ),
        )
        return list(response.results)

    async def read_derivation(self, drv_path: str | StorePath, /) -> Derivation:
        return await self.rpc.read_derivation(ReadDerivationRequest(path=str(drv_path)))

    async def write_dev_shell_derivation(self, drv_path: str | StorePath, get_env_script: str, /) -> str:
        response = await self.rpc.write_dev_shell_derivation(
            WriteDevShellDerivationRequest(path=str(drv_path), get_env_script=get_env_script),
        )
        return response.path

    @no_runtime_type_check  # action validates its own membership in GcAction at
    # runtime for untyped callers (see the guard below); beartype's parameter
    # check would otherwise intercept before that guard runs and raise its own
    # exception type instead of the documented ValueError. `_core/_objects.py`
    # carries the same decorator, for the same reason.
    async def collect_garbage(
        self,
        action: GcAction,
        /,
        *,
        ignore_liveness: bool = False,
        paths_to_delete: list[str | StorePath] | tuple[()] = (),
        max_freed: int = NO_GC_LIMIT,
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

        Raises:
            ValueError: The action is not a member of
                :class:`~nanopynix_proto.nix.store.GcAction`.
        """
        # **This guard used to be a side effect of one protoc option.**
        # `nanopynix-proto/generated.nix` passed `pydantic_dataclasses`, so
        # `CollectGarbageRequest(action=...)` below raised a pydantic
        # `ValidationError` for an unmapped action, and that class subclasses
        # `ValueError`. Issue #127 removed the option, which costs 0.254 s of
        # import time in every process that speaks the protocol, and this
        # method is the one caller that read the validation. The check is now
        # explicit, and it says the same thing as the inproc engine.
        if action not in GcAction:
            raise ValueError(f"unsupported garbage-collection action: {action!r}")
        response = await self.rpc.collect_garbage(
            CollectGarbageRequest(
                action=action,
                ignore_liveness=ignore_liveness,
                paths_to_delete=[str(p) for p in paths_to_delete],
                max_freed=max_freed,
            ),
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

    async def copy_closure(
        self,
        paths: list[str | StorePath],
        /,
        dest_store: Store,
        *,
        repair: bool = False,
        check_sigs: bool = True,
        substitute: bool = False,
    ) -> None:
        """Copy the closure of ``paths`` from this store to ``dest_store``."""
        if dest_store._session_id != self._session_id:
            raise ValueError("dest_store belongs to a different Session")
        await self.rpc.copy_closure(
            CopyClosureRequest(
                paths=[str(path) for path in paths],
                dest_store_handle=dest_store._store_handle,
                repair=repair,
                check_sigs=check_sigs,
                substitute=substitute,
            ),
        )

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
        method: str = DEFAULT_CA_METHOD,
        hash_algo: str = DEFAULT_HASH_ALGO,
    ) -> StorePath:
        response = await self.rpc.compute_store_path(
            ComputeStorePathRequest(path=path, name=name, method=method, hash_algo=hash_algo),
        )
        return StorePath(response.path)

    async def add_to_store(
        self,
        path: str,
        *,
        name: str | None = None,
        method: str = DEFAULT_CA_METHOD,
        hash_algo: str = DEFAULT_HASH_ALGO,
    ) -> StorePath:
        """Add a file or directory to this store."""
        response = await self.rpc.add_to_store(
            AddToStoreRequest(path=path, name=name, method=method, hash_algo=hash_algo),
        )
        return StorePath(response.path)
