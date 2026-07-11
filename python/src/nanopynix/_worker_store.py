"""gRPC StoreService handler for the worker subprocess."""

from __future__ import annotations

from typing import Any

import nanopynix_fetchers
import nanopynix_store
from nanopynix._extract import (
    input_attrs as _input_attrs,
    store_path as _sp_to_pb,
    store_path_str as _sp_str_to_pb,
)
from nanopynix._grpc_util import wrap_service_handlers
from nanopynix_proto.nix import common as common_pb
from nanopynix_proto.nix.store import (
    AddTempRootRequest,
    AddTempRootResponse,
    BuildDerivationRequest,
    BuildPathsWithResultsRequest,
    ComputeFsClosureRequest,
    FetchFromAttrsRequest,
    FetchFromUrlRequest,
    FollowLinksToStorePathRequest,
    GetStoreDirRequest,
    GetStoreDirResponse,
    GetUriRequest,
    GetUriResponse,
    IsValidPathRequest,
    IsValidPathResponse,
    ParseStorePathRequest,
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
)

# ── helpers ──────────────────────────────────────────────────────────


def _sp(pb_or_str: common_pb.StorePath | str) -> Any:
    """Return either a string or a parsed C++ StorePath for the store API.

    The caller has already resolved relative paths into absolute ones, so
    we munge it into the format expected by the store methods.
    """
    # Handlers resolve paths before calling this helper.
    return pb_or_str


def _parse_sp(path: str, store: Any) -> Any:
    """Parse a store path string into a C++ StorePath object."""
    if not path.startswith("/"):
        path = f"{store.get_store_dir()}/{path}"
    return store.parse_store_path(path)


def _pb_store_path(sp_obj: Any) -> common_pb.StorePath:
    """Convert a C++ StorePath object (or dict with same keys) to proto."""
    if isinstance(sp_obj, str):
        return _sp_str_to_pb(sp_obj)
    if hasattr(sp_obj, "to_string"):
        return _sp_to_pb(sp_obj)
    # Fallback: dict-like with keys
    return common_pb.StorePath(
        to_string=str(sp_obj["to_string"]),
        hash_part=str(sp_obj["hash_part"]),
        name=str(sp_obj["name"]),
    )


def _pb_store_path_list(objs: Any) -> common_pb.StorePathList:
    """Convert a list of C++ StorePath objects (or strings/dicts) to StorePathList."""
    paths = [_pb_store_path(p) for p in objs]
    return common_pb.StorePathList(paths=paths)


def _attrs_value_to_str(v: common_pb.AttrsValue) -> str:
    """Extract a plain string from an AttrsValue proto for the fetcher API."""
    if v.string_value is not None:
        return v.string_value
    if v.int_value is not None:
        return str(v.int_value)
    if v.bool_value is not None:
        return str(v.bool_value).lower()
    raise ValueError(f"cannot convert AttrsValue to string: {v!r}")


# ── Service handler ──────────────────────────────────────────────────


@wrap_service_handlers
class StoreServiceHandler(StoreServiceBase):
    """gRPC handler for all store operations."""

    def __init__(self, state: Any) -> None:
        self._state = state

    @property
    def _store(self) -> Any:
        if self._state.store is None:
            raise RuntimeError("store not initialized")
        return self._state.store

    # ── simple accessors ──────────────────────────────────────────

    async def get_uri(self, message: GetUriRequest) -> GetUriResponse:
        return GetUriResponse(uri=self._store.get_uri())

    async def get_store_dir(self, message: GetStoreDirRequest) -> GetStoreDirResponse:
        return GetStoreDirResponse(dir=self._store.get_store_dir())

    async def is_valid_path(self, message: IsValidPathRequest) -> IsValidPathResponse:
        sp = _parse_sp(message.path, self._store)
        return IsValidPathResponse(valid=self._store.is_valid_path(sp))

    async def parse_store_path(self, message: ParseStorePathRequest) -> common_pb.StorePath:
        sp = _parse_sp(message.path, self._store)
        return _sp_to_pb(sp)

    # ── path info ─────────────────────────────────────────────────

    async def query_path_info(self, message: QueryPathInfoRequest) -> common_pb.PathInfo:
        sp = _parse_sp(message.path, self._store)
        info = dict(self._store.query_path_info(sp))
        return common_pb.PathInfo(
            path=_pb_store_path(info.get("path")),
            nar_hash=str(info.get("nar_hash", "")),
            nar_size=int(info.get("nar_size", 0)),
            registration_time=info.get("registration_time"),
            deriver=_pb_store_path(info["deriver"]) if info.get("deriver") else None,
            references=[_pb_store_path(p) for p in info.get("references", [])],
            ca=info.get("ca"),
            ultimate=bool(info.get("ultimate", False)),
        )

    async def query_path_from_hash_part(
        self, message: QueryPathFromHashPartRequest
    ) -> QueryPathFromHashPartResponse:
        sp = self._store.query_path_from_hash_part(message.hash_part)
        return QueryPathFromHashPartResponse(
            path=_sp_to_pb(sp) if sp is not None else None,
        )

    # ── closure / traversal ───────────────────────────────────────

    async def compute_fs_closure(
        self, message: ComputeFsClosureRequest
    ) -> common_pb.StorePathList:
        sp = _parse_sp(message.path, self._store)
        objs = self._store.compute_fs_closure(
            sp,
            message.flip_direction,
            message.include_outputs,
            message.include_derivers,
        )
        return _pb_store_path_list(objs)

    async def query_missing(self, message: QueryMissingRequest) -> common_pb.MissingInfo:
        sps = [_parse_sp(p, self._store) for p in message.paths]
        info = dict(self._store.query_missing(sps))
        return common_pb.MissingInfo(
            will_build=[_pb_store_path(p) for p in info.get("will_build", [])],
            will_substitute=[_pb_store_path(p) for p in info.get("will_substitute", [])],
            unknown=[_pb_store_path(p) for p in info.get("unknown", [])],
            download_size=int(info.get("download_size", 0)),
            nar_size=int(info.get("nar_size", 0)),
        )

    async def query_derivation_outputs(
        self, message: QueryDerivationOutputsRequest
    ) -> common_pb.StorePathList:
        sp = _parse_sp(message.path, self._store)
        objs = self._store.query_derivation_outputs(sp)
        return _pb_store_path_list(objs)

    async def query_valid_derivers(
        self, message: QueryValidDeriversRequest
    ) -> common_pb.StorePathList:
        sp = _parse_sp(message.path, self._store)
        objs = self._store.query_valid_derivers(sp)
        return _pb_store_path_list(objs)

    async def query_all_valid_paths(
        self, message: QueryAllValidPathsRequest
    ) -> common_pb.StorePathList:
        objs = self._store.query_all_valid_paths()
        return _pb_store_path_list(objs)

    async def query_referrers(self, message: QueryReferrersRequest) -> common_pb.StorePathList:
        sp = _parse_sp(message.path, self._store)
        objs = self._store.query_referrers(sp)
        return _pb_store_path_list(objs)

    async def query_substitutable_paths(
        self, message: QuerySubstitutablePathsRequest
    ) -> common_pb.StorePathList:
        sps = [_parse_sp(p, self._store) for p in message.paths]
        objs = self._store.query_substitutable_paths(sps)
        return _pb_store_path_list(objs)

    # ── building ──────────────────────────────────────────────────

    async def build_paths_with_results(
        self, message: BuildPathsWithResultsRequest
    ) -> common_pb.BuildResultList:
        sps = [_parse_sp(p, self._store) for p in message.paths]
        results = list(self._store.build_paths_with_results(sps, self._state.eval_store))
        br_list = [_dict_to_build_result(r) for r in results]
        return common_pb.BuildResultList(results=br_list)

    async def read_derivation(self, message: ReadDerivationRequest) -> common_pb.Derivation:
        sp = _parse_sp(message.path, self._store)
        raw = dict(self._store.read_derivation(sp))
        return _dict_to_derivation(raw)

    async def build_derivation(
        self, message: BuildDerivationRequest
    ) -> common_pb.BuildResult:
        sp = _parse_sp(message.path, self._store)
        result = dict(self._store.build_derivation(sp, nanopynix_store.BuildMode(message.build_mode)))
        return _dict_to_build_result(result)

    # ── links / roots ─────────────────────────────────────────────

    async def follow_links_to_store_path(
        self, message: FollowLinksToStorePathRequest
    ) -> common_pb.StorePath:
        sp = self._store.follow_links_to_store_path(message.path)
        return _sp_to_pb(sp)

    async def add_temp_root(self, message: AddTempRootRequest) -> AddTempRootResponse:
        sp = _parse_sp(message.path, self._store)
        self._store.add_temp_root(sp)
        return AddTempRootResponse()

    # ── fetchers ──────────────────────────────────────────────────

    async def fetch_from_url(self, message: FetchFromUrlRequest) -> common_pb.Input:
        inp = nanopynix_fetchers.input_from_url(message.url)
        return common_pb.Input(attrs=_input_attrs(inp))

    async def fetch_from_attrs(self, message: FetchFromAttrsRequest) -> common_pb.Input:
        attrs = {k: _attrs_value_to_str(v) for k, v in message.attrs.items()}
        inp = nanopynix_fetchers.input_from_attrs(attrs)
        return common_pb.Input(attrs=_input_attrs(inp))


# ── dict → proto converters ──────────────────────────────────────────


def _dict_to_build_result(d: dict[str, Any]) -> common_pb.BuildResult:
    return common_pb.BuildResult(
        drv_path=str(d.get("drv_path", "")),
        success=bool(d.get("success", False)),
        status=str(d.get("status", "")),
        error_msg=str(d.get("error_msg", "")),
    )


def _dict_to_derivation(d: dict[str, Any]) -> common_pb.Derivation:
    # env can be a list of [key, value] pairs or a dict
    env_raw = d.get("env", {})
    if isinstance(env_raw, list):
        env = {str(k): str(v) for k, v in env_raw}
    else:
        env = {str(k): str(v) for k, v in env_raw.items()}

    # input_drvs can be a list of {path, outputs, children} or a dict
    idrvs_raw = d.get("input_drvs", {})
    if isinstance(idrvs_raw, list):
        input_drvs: dict[str, common_pb.DerivationOutputs] = {}
        for entry in idrvs_raw:
            path = str(entry.get("path", ""))
            children = dict(entry.get("children", {}))
            input_drvs[path] = common_pb.DerivationOutputs(
                outputs=[str(o) for o in entry.get("outputs", [])],
                dynamic_outputs={str(k): str(v) for k, v in children.items()},
            )
    else:
        input_drvs = {
            str(k): common_pb.DerivationOutputs(
                outputs=[str(o) for o in v.outputs] if hasattr(v, "outputs") else [],
                dynamic_outputs={
                    str(kk): str(vv) for kk, vv in (
                        v.dynamic_outputs.items() if hasattr(v, "dynamic_outputs") else {}
                    )
                },
            )
            for k, v in idrvs_raw.items()
        }

    return common_pb.Derivation(
        name=str(d.get("name", "")),
        system=str(d.get("system", d.get("platform", ""))),
        builder=str(d.get("builder", "")),
        args=[str(a) for a in d.get("args", [])],
        env=env,
        input_drvs=input_drvs,
        input_srcs=[str(s) for s in d.get("input_srcs", [])],
    )
