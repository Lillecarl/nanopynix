"""Store RPC dispatch for the worker subprocess."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter

import nanopynix_fetchers
import nanopynix_store
from nanopynix import _protocol as rpc
from nanopynix._extract import input_attrs as _input_attrs
from nanopynix._extract import store_path as _sp_to_dict
from nanopynix._worker_common import Endpoint, dispatch
from nanopynix.models import Input, StorePath

if TYPE_CHECKING:
    from collections.abc import Mapping

_StorePathList = TypeAdapter(list[StorePath])


def store_dispatch(store, eval_store):
    """Return dispatch dict for store operations."""

    def _parse_sp(path: str):
        if not path.startswith("/"):
            path = f"{store.get_store_dir()}/{path}"
        return store.parse_store_path(path)

    def _store_path_list(paths):
        paths_model = _StorePathList.validate_python([p if isinstance(p, dict) else _sp_to_dict(p) for p in paths])
        return _StorePathList.dump_python(paths_model, mode="json")

    def _fetcher_attrs(attrs: Mapping[str, str | int | bool]) -> dict[str, str]:
        return {key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in attrs.items()}

    def get_uri(_: rpc.GetUri) -> str:
        return store.get_uri()

    def get_store_dir(_: rpc.GetStoreDir) -> str:
        return store.get_store_dir()

    def is_valid_path(req: rpc.IsValidPath) -> bool:
        return store.is_valid_path(_parse_sp(req.path))

    def parse_store_path(req: rpc.ParseStorePath):
        return _sp_to_dict(_parse_sp(req.path))

    def query_path_info(req: rpc.QueryPathInfo):
        return dict(store.query_path_info(_parse_sp(req.path)))

    def query_path_from_hash_part(req: rpc.QueryPathFromHashPart):
        sp = store.query_path_from_hash_part(req.hash_part)
        return _sp_to_dict(sp) if sp is not None else None

    def compute_fs_closure(req: rpc.ComputeFsClosure):
        return _store_path_list(
            store.compute_fs_closure(
                _parse_sp(req.path),
                req.flip_direction,
                req.include_outputs,
                req.include_derivers,
            )
        )

    def query_missing(req: rpc.QueryMissing):
        return dict(store.query_missing([_parse_sp(path) for path in req.paths]))

    def query_derivation_outputs(req: rpc.QueryDerivationOutputs):
        return _store_path_list(store.query_derivation_outputs(_parse_sp(req.path)))

    def query_valid_derivers(req: rpc.QueryValidDerivers):
        return _store_path_list(store.query_valid_derivers(_parse_sp(req.path)))

    def query_all_valid_paths(_: rpc.QueryAllValidPaths):
        return _store_path_list(store.query_all_valid_paths())

    def query_referrers(req: rpc.QueryReferrers):
        return _store_path_list(store.query_referrers(_parse_sp(req.path)))

    def query_substitutable_paths(req: rpc.QuerySubstitutablePaths):
        return _store_path_list(store.query_substitutable_paths([_parse_sp(path) for path in req.paths]))

    def build_paths_with_results(req: rpc.BuildPathsWithResults):
        return list(
            store.build_paths_with_results(
                [_parse_sp(path) for path in req.paths],
                eval_store,
            )
        )

    def read_derivation(req: rpc.ReadDerivation):
        return dict(store.read_derivation(_parse_sp(req.path)))

    def build_derivation(req: rpc.BuildDerivation):
        return store.build_derivation(
            _parse_sp(req.path),
            nanopynix_store.BuildMode(req.build_mode),
        )

    def follow_links_to_store_path(req: rpc.FollowLinksToStorePath):
        return _sp_to_dict(store.follow_links_to_store_path(req.path))

    def add_temp_root(req: rpc.AddTempRoot):
        return store.add_temp_root(_parse_sp(req.path))

    def fetch_from_url(req: rpc.FetchFromUrl) -> Input:
        return Input(attrs=_input_attrs(nanopynix_fetchers.input_from_url(req.url)))

    def fetch_from_attrs(req: rpc.FetchFromAttrs) -> Input:
        return Input(attrs=_input_attrs(nanopynix_fetchers.input_from_attrs(_fetcher_attrs(req.attrs))))

    return dispatch(
        [
            Endpoint(rpc.GetUri, get_uri),
            Endpoint(rpc.GetStoreDir, get_store_dir),
            Endpoint(rpc.IsValidPath, is_valid_path),
            Endpoint(rpc.ParseStorePath, parse_store_path),
            Endpoint(rpc.QueryPathInfo, query_path_info),
            Endpoint(rpc.QueryPathFromHashPart, query_path_from_hash_part),
            Endpoint(rpc.ComputeFsClosure, compute_fs_closure),
            Endpoint(rpc.QueryMissing, query_missing),
            Endpoint(rpc.QueryDerivationOutputs, query_derivation_outputs),
            Endpoint(rpc.QueryValidDerivers, query_valid_derivers),
            Endpoint(rpc.QueryAllValidPaths, query_all_valid_paths),
            Endpoint(rpc.QueryReferrers, query_referrers),
            Endpoint(rpc.QuerySubstitutablePaths, query_substitutable_paths),
            Endpoint(rpc.BuildPathsWithResults, build_paths_with_results),
            Endpoint(rpc.ReadDerivation, read_derivation),
            Endpoint(rpc.BuildDerivation, build_derivation),
            Endpoint(rpc.FollowLinksToStorePath, follow_links_to_store_path),
            Endpoint(rpc.AddTempRoot, add_temp_root),
            Endpoint(rpc.FetchFromUrl, fetch_from_url),
            Endpoint(rpc.FetchFromAttrs, fetch_from_attrs),
        ]
    )
