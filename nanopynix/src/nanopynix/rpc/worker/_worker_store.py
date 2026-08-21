"""gRPC StoreService handler for the worker subprocess."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanopynix_proto.nix.common import (
    BuildResultList,
    Derivation,
    GcRoot,
    MissingInfo,
    PathInfo,
    StoreDirs,
)
from nanopynix_proto.nix.store import (
    AddIndirectRootRequest,
    AddIndirectRootResponse,
    AddPermRootRequest,
    AddPermRootResponse,
    AddTempRootRequest,
    AddTempRootResponse,
    AddToStoreRequest,
    AddToStoreResponse,
    BuildPathsWithResultsRequest,
    CollectGarbageRequest,
    CollectGarbageResponse,
    ComputeFsClosureRequest,
    ComputeStorePathRequest,
    ComputeStorePathResponse,
    CopyClosureRequest,
    CopyClosureResponse,
    DumpDbRequest,
    DumpDbResponse,
    EnsurePathRequest,
    EnsurePathResponse,
    FindRootsRequest,
    FindRootsResponse,
    FollowLinksToStorePathRequest,
    FollowLinksToStorePathResponse,
    GetBuildLogRequest,
    GetBuildLogResponse,
    GetStoreDirRequest,
    GetStoreDirResponse,
    GetStoreDirsRequest,
    GetUriRequest,
    GetUriResponse,
    IsValidPathRequest,
    IsValidPathResponse,
    ListRegistryEntriesRequest,
    ListRegistryEntriesResponse,
    OptimiseStoreRequest,
    OptimiseStoreResponse,
    ParseStorePathRequest,
    ParseStorePathResponse,
    PathsResponse,
    QueryAllValidPathsRequest,
    QueryDerivationOutputsRequest,
    QueryMissingRequest,
    QueryPathFromHashPartRequest,
    QueryPathFromHashPartResponse,
    QueryPathInfoRequest,
    QueryReferrersRequest,
    QuerySubstitutablePathsRequest,
    QueryValidDeriversRequest,
    ReadDerivationRequest,
    StoreServiceBase,
    VerifyStoreRequest,
    VerifyStoreResponse,
    WriteDevShellDerivationRequest,
    WriteDevShellDerivationResponse,
)

from nanopynix._typechecking import BEARTYPING
from nanopynix.rpc.worker._grpc_util import worker_op, wrap_service_handlers

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

    from nanopynix._core._objects import CoreStore
    from nanopynix.rpc.worker._state import WorkerState


def _widen_paths(paths: Sequence[str]) -> list[str]:
    """Re-type a ``list[StorePath]`` as the ``list[str]`` the proto declares.

    ``StorePath`` subclasses ``str``, so this is a no-op at runtime -- but
    ``list`` is invariant, so ``list[StorePath]`` is not a ``list[str]`` and
    the assignment needs a widening step. ``Sequence`` is covariant, which is
    what lets the parameter accept either.
    """
    return list(paths)


@wrap_service_handlers
class StoreServiceHandler(StoreServiceBase):
    """gRPC handler backed by the shared, typed :class:`CoreStore`.

    One method per RPC, the shape ``_worker_eval.py`` already used. There is
    no reflective forwarder and no proto-dict marshalling step: the request
    message is read directly and handed to the same ``CoreStore`` method the
    inproc engine calls, so the two engines cannot disagree about what an
    operation means.

    Store operations dispatch to the worker's bounded Store pool, keeping the
    event loop free while allowing independent Store calls to overlap.
    """

    def __init__(self, state: WorkerState) -> None:
        self._state: WorkerState = state

    def _resolve(self, store_handle: int) -> CoreStore:
        return self._state.handles.get_store(store_handle)

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

    # Each handler is written synchronously and made async by @_store_op.

    @worker_op
    def is_valid_path(self, message: IsValidPathRequest) -> IsValidPathResponse:
        return IsValidPathResponse(valid=self._resolve(message.store_handle).is_valid_path(message.path))

    @worker_op
    def query_missing(self, message: QueryMissingRequest) -> MissingInfo:
        return self._resolve(message.store_handle).query_missing(list(message.derived_paths))

    @worker_op
    def build_paths_with_results(self, message: BuildPathsWithResultsRequest) -> BuildResultList:
        return BuildResultList(
            results=self._resolve(message.store_handle).build_paths_with_results(
                list(message.derived_paths),
                build_mode=message.build_mode,
                eval_store=self._resolve(message.eval_store_handle) if message.eval_store_handle else None,
            ),
        )

    @worker_op
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

    # --- Identity ---------------------------------------------------------

    @worker_op
    def get_uri(self, message: GetUriRequest) -> GetUriResponse:
        return GetUriResponse(uri=self._resolve(message.store_handle).get_uri(with_params=message.with_params))

    @worker_op
    def get_store_dir(self, message: GetStoreDirRequest) -> GetStoreDirResponse:
        return GetStoreDirResponse(dir=self._resolve(message.store_handle).get_store_dir())

    @worker_op
    def get_store_dirs(self, message: GetStoreDirsRequest) -> StoreDirs:
        return self._resolve(message.store_handle).get_store_dirs()

    @worker_op
    def parse_store_path(self, message: ParseStorePathRequest) -> ParseStorePathResponse:
        return ParseStorePathResponse(path=self._resolve(message.store_handle).parse_store_path(message.path))

    @worker_op
    def follow_links_to_store_path(self, message: FollowLinksToStorePathRequest) -> FollowLinksToStorePathResponse:
        return FollowLinksToStorePathResponse(
            path=self._resolve(message.store_handle).follow_links_to_store_path(message.path),
        )

    @worker_op
    def query_path_from_hash_part(self, message: QueryPathFromHashPartRequest) -> QueryPathFromHashPartResponse:
        return QueryPathFromHashPartResponse(
            path=self._resolve(message.store_handle).query_path_from_hash_part(message.hash_part),
        )

    # --- Queries ----------------------------------------------------------

    @worker_op
    def query_path_info(self, message: QueryPathInfoRequest) -> PathInfo:
        return self._resolve(message.store_handle).query_path_info(message.path)

    @worker_op
    def query_all_valid_paths(self, message: QueryAllValidPathsRequest) -> PathsResponse:
        return PathsResponse(paths=_widen_paths(self._resolve(message.store_handle).query_all_valid_paths()))

    @worker_op
    def compute_fs_closure(self, message: ComputeFsClosureRequest) -> PathsResponse:
        return PathsResponse(
            paths=_widen_paths(
                self._resolve(message.store_handle).compute_fs_closure(
                    message.path,
                    flip_direction=message.flip_direction,
                    include_outputs=message.include_outputs,
                    include_derivers=message.include_derivers,
                ),
            ),
        )

    @worker_op
    def query_derivation_outputs(self, message: QueryDerivationOutputsRequest) -> PathsResponse:
        return PathsResponse(
            paths=_widen_paths(self._resolve(message.store_handle).query_derivation_outputs(message.path))
        )

    @worker_op
    def query_valid_derivers(self, message: QueryValidDeriversRequest) -> PathsResponse:
        return PathsResponse(paths=_widen_paths(self._resolve(message.store_handle).query_valid_derivers(message.path)))

    @worker_op
    def query_referrers(self, message: QueryReferrersRequest) -> PathsResponse:
        return PathsResponse(paths=_widen_paths(self._resolve(message.store_handle).query_referrers(message.path)))

    @worker_op
    def query_substitutable_paths(self, message: QuerySubstitutablePathsRequest) -> PathsResponse:
        return PathsResponse(
            paths=_widen_paths(self._resolve(message.store_handle).query_substitutable_paths(list(message.paths))),
        )

    @worker_op
    def dump_db(self, message: DumpDbRequest) -> DumpDbResponse:
        return DumpDbResponse(
            registration=self._resolve(message.store_handle).dump_db(
                list(message.paths),
                show_derivers=message.show_derivers,
                show_hash=message.show_hash,
            ),
        )

    @worker_op
    def get_build_log(self, message: GetBuildLogRequest) -> GetBuildLogResponse:
        return GetBuildLogResponse(log=self._resolve(message.store_handle).get_build_log(message.path))

    @worker_op
    def read_derivation(self, message: ReadDerivationRequest) -> Derivation:
        return self._resolve(message.store_handle).read_derivation(message.path)

    # --- Mutation ---------------------------------------------------------

    @worker_op
    def write_dev_shell_derivation(self, message: WriteDevShellDerivationRequest) -> WriteDevShellDerivationResponse:
        return WriteDevShellDerivationResponse(
            path=self._resolve(message.store_handle).write_dev_shell_derivation(
                message.path,
                message.get_env_script,
            ),
        )

    @worker_op
    def ensure_path(self, message: EnsurePathRequest) -> EnsurePathResponse:
        self._resolve(message.store_handle).ensure_path(message.path)
        return EnsurePathResponse()

    @worker_op
    def add_to_store(self, message: AddToStoreRequest) -> AddToStoreResponse:
        return AddToStoreResponse(
            path=self._resolve(message.store_handle).add_to_store(
                message.path,
                name=message.name,
                method=message.method,
                hash_algo=message.hash_algo,
            ),
        )

    @worker_op
    def compute_store_path(self, message: ComputeStorePathRequest) -> ComputeStorePathResponse:
        return ComputeStorePathResponse(
            path=self._resolve(message.store_handle).compute_store_path(
                message.path,
                name=message.name,
                method=message.method,
                hash_algo=message.hash_algo,
            ),
        )

    @worker_op
    def optimise_store(self, message: OptimiseStoreRequest) -> OptimiseStoreResponse:
        self._resolve(message.store_handle).optimise_store()
        return OptimiseStoreResponse()

    @worker_op
    def verify_store(self, message: VerifyStoreRequest) -> VerifyStoreResponse:
        return VerifyStoreResponse(
            errors=self._resolve(message.store_handle).verify_store(
                check_contents=message.check_contents, repair=message.repair
            ),
        )

    # --- Garbage collection -----------------------------------------------

    @worker_op
    def add_temp_root(self, message: AddTempRootRequest) -> AddTempRootResponse:
        self._resolve(message.store_handle).add_temp_root(message.path)
        return AddTempRootResponse()

    @worker_op
    def add_perm_root(self, message: AddPermRootRequest) -> AddPermRootResponse:
        return AddPermRootResponse(
            path=self._resolve(message.store_handle).add_perm_root(message.store_path, message.gc_root),
        )

    @worker_op
    def add_indirect_root(self, message: AddIndirectRootRequest) -> AddIndirectRootResponse:
        self._resolve(message.store_handle).add_indirect_root(message.path)
        return AddIndirectRootResponse()

    @worker_op
    def find_roots(self, message: FindRootsRequest) -> FindRootsResponse:
        return FindRootsResponse(
            roots=[
                GcRoot(link=root.link, path=root.path)
                for root in self._resolve(message.store_handle).find_roots(censor=message.censor)
            ],
        )

    @worker_op
    def list_registry_entries(self, message: ListRegistryEntriesRequest) -> ListRegistryEntriesResponse:
        return ListRegistryEntriesResponse(
            entries=self._resolve(message.store_handle).registry_entries(fetch_settings=message.fetch_settings),
        )

    @worker_op
    def collect_garbage(self, message: CollectGarbageRequest) -> CollectGarbageResponse:
        result = self._resolve(message.store_handle).collect_garbage(
            message.action,
            ignore_liveness=message.ignore_liveness,
            paths_to_delete=list(message.paths_to_delete),
            max_freed=message.max_freed,
        )
        return CollectGarbageResponse(paths=_widen_paths(result.paths), bytes_freed=result.bytes_freed)
