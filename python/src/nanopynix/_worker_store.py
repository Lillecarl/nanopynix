"""gRPC StoreService handler for the worker subprocess."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

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

import nanopynix_fetchers
import nanopynix_store
from nanopynix._extract import (
    input_attrs as _input_attrs,
)
from nanopynix._extract import (
    store_path as _sp_to_pb,
)
from nanopynix._extract import (
    store_path_str as _sp_str_to_pb,
)
from nanopynix._grpc_util import wrap_service_handlers

# ── helpers ──────────────────────────────────────────────────────────

MessageT = TypeVar("MessageT")


def _parse_sp(path: str, store: Any) -> Any:
    """Parse a store path string into a C++ StorePath object."""
    if not path.startswith("/"):
        path = f"{store.get_store_dir()}/{path}"
    return store.parse_store_path(path)


def _pb_store_path(sp_obj: Any) -> common_pb.StorePath:
    """Convert a C++ StorePath object (or dict with same keys) to proto."""
    return common_pb.StorePath.from_dict(_proto_shape_store_path(sp_obj))


def _pb_store_path_list(objs: Any) -> common_pb.StorePathList:
    """Convert a list of C++ StorePath objects (or strings/dicts) to StorePathList."""
    return common_pb.StorePathList.from_dict({"paths": [_proto_shape_store_path(p) for p in objs]})


def _attrs_value_to_str(v: common_pb.AttrsValue) -> str:
    """Extract a plain string from an AttrsValue proto for the fetcher API."""
    if v.string_value is not None:
        return v.string_value
    if v.int_value is not None:
        return str(v.int_value)
    if v.bool_value is not None:
        return str(v.bool_value).lower()
    raise ValueError(f"cannot convert AttrsValue to string: {v!r}")


def _message_from_nanobind(cls: type[MessageT], value: Any) -> MessageT:
    """Validate a nanobind response through the generated betterproto2 dataclass."""
    return cls.from_dict(_proto_shape(value))  # type: ignore[attr-defined, no-any-return]


def _proto_shape(value: Any) -> Any:
    """Normalize residual nanobind values into proto-shaped plain Python data."""
    if isinstance(value, Mapping):
        return {str(k): _proto_shape(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_proto_shape(v) for v in value]
    if _is_store_path_like(value):
        return _proto_shape_store_path(value)
    return value


def _proto_shape_store_path(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        return _sp_str_to_pb(value).to_dict()
    if isinstance(value, Mapping):
        return {
            "to_string": str(value["to_string"]),
            "hash_part": str(value["hash_part"]),
            "name": str(value["name"]),
        }
    if _is_store_path_like(value):
        return _sp_to_pb(value).to_dict()
    raise TypeError(f"cannot convert StorePath-like value to proto dict: {value!r}")


def _is_store_path_like(value: Any) -> bool:
    return (
        hasattr(value, "to_string")
        and hasattr(value, "hash_part")
        and hasattr(value, "name")
    )


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
        return _message_from_nanobind(common_pb.PathInfo, self._store.query_path_info(sp))

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
        return _message_from_nanobind(common_pb.MissingInfo, self._store.query_missing(sps))

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
        return common_pb.BuildResultList.from_dict({"results": [_proto_shape(r) for r in results]})

    async def read_derivation(self, message: ReadDerivationRequest) -> common_pb.Derivation:
        sp = _parse_sp(message.path, self._store)
        return common_pb.Derivation.from_dict(_proto_shape_derivation(self._store.read_derivation(sp)))

    async def build_derivation(
        self, message: BuildDerivationRequest
    ) -> common_pb.BuildResult:
        sp = _parse_sp(message.path, self._store)
        result = self._store.build_derivation(sp, nanopynix_store.BuildMode(message.build_mode))
        return _message_from_nanobind(common_pb.BuildResult, result)

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


def _proto_shape_derivation(value: Any) -> dict[str, Any]:
    d = dict(_proto_shape(value))
    # env can be a list of [key, value] pairs or a dict
    env_raw = d.get("env", {})
    if isinstance(env_raw, list):
        env = {str(k): str(v) for k, v in env_raw}
    else:
        env = {str(k): str(v) for k, v in env_raw.items()}

    # input_drvs can be a list of {path, outputs, children} or a dict
    idrvs_raw = d.get("input_drvs", {})
    if isinstance(idrvs_raw, list):
        input_drvs: dict[str, dict[str, Any]] = {}
        for entry in idrvs_raw:
            path = str(entry.get("path", ""))
            children = dict(entry.get("children", {}))
            input_drvs[path] = {
                "outputs": [str(o) for o in entry.get("outputs", [])],
                "dynamic_outputs": {str(k): str(v) for k, v in children.items()},
            }
    else:
        input_drvs = {
            str(k): {
                "outputs": [str(o) for o in v.outputs] if hasattr(v, "outputs") else [],
                "dynamic_outputs": {
                    str(kk): str(vv) for kk, vv in (
                        v.dynamic_outputs.items() if hasattr(v, "dynamic_outputs") else {}
                    )
                },
            }
            for k, v in idrvs_raw.items()
        }

    return {
        "name": str(d.get("name", "")),
        "system": str(d.get("system", d.get("platform", ""))),
        "builder": str(d.get("builder", "")),
        "args": [str(a) for a in d.get("args", [])],
        "env": env,
        "input_drvs": input_drvs,
        "input_srcs": [str(s) for s in d.get("input_srcs", [])],
    }
