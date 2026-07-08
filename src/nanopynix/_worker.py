"""Subprocess worker — Nix execution over stdin/stdout JSON-RPC 2.0.

Spawned by ``Session._WorkerManager`` via ``asyncio.create_subprocess_exec``.
Reads JSON-RPC requests from stdin, writes responses and log-event
notifications to stdout.  One line per message (compact JSON, no embedded
newlines).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, cast

import nanopynix_expr
import nanopynix_fetchers
import nanopynix_flake
import nanopynix_store
import nanopynix_util
from pydantic import TypeAdapter

import nanopynix._protocol as rpc
from nanopynix._extract import (
    flake_ref_attrs as _flake_ref_attrs,
)
from nanopynix._extract import (
    input_attrs as _input_attrs,
)
from nanopynix._extract import (
    locked_flake as _locked_flake,
)
from nanopynix._extract import (
    store_path as _sp_to_dict,
)
from nanopynix.logging import LogCollector
from nanopynix.models import (
    AttrsCallArg,
    DeepAttrs,
    DeepList,
    DeepScalar,
    FlakeRef,
    Input,
    ListCallArg,
    PrimOpSpec,
    RemoteCallArg,
    RemoteValueRef,
    ScalarCallArg,
    StorePath,
    ValueHandle,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_StorePathList = TypeAdapter(list[StorePath])
ReqT = TypeVar("ReqT", bound=rpc.WorkerRequest[Any])


# ── Wire format ────────────────────────────────────────────────────────


def _send(msg: dict) -> None:
    """Write a JSON-RPC message as a single line to stdout and flush."""
    sys.stdout.write(json.dumps(msg, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _error(req_id, code: int, message: str, data=None) -> dict:
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message, "data": data},
        "id": req_id,
    }


def _result(req_id, value) -> dict:
    return {"jsonrpc": "2.0", "result": value, "id": req_id}


def _notification(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}


# ── Endpoint registry ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Endpoint[ReqT: rpc.WorkerRequest[Any]]:
    """Worker RPC endpoint bound to a typed request model."""

    request: type[ReqT]
    handler: Callable[[ReqT], Any]

    @property
    def name(self) -> str:
        return self.request.method

    def __call__(self, args: list[Any]) -> Any:
        request = self.request.from_args(args)
        return self.request.dump_response(self.handler(request))


def _dispatch(endpoints: list[Endpoint[Any]]) -> dict[str, Endpoint[Any]]:
    return {endpoint.name: endpoint for endpoint in endpoints}


def _import_callable(import_path: str) -> Callable[..., Any]:
    module_name, sep, attr_path = import_path.partition(":")
    if not sep or not module_name or not attr_path:
        raise ValueError(f"invalid primop import path: {import_path!r}")
    value: Any = importlib.import_module(module_name)
    for attr in attr_path.split("."):
        value = getattr(value, attr)
    if not callable(value):
        raise TypeError(f"primop import path is not callable: {import_path!r}")
    return value


def _register_primops(raw_specs: list[dict[str, Any]]) -> None:
    for raw in raw_specs:
        spec = PrimOpSpec.model_validate(raw)
        nanopynix_expr.register_primop(
            spec.name,
            spec.arity,
            spec.args,
            spec.doc,
            _import_callable(spec.import_path),
        )


# ── Main ───────────────────────────────────────────────────────────────


def main() -> None:
    """Bootstrap Nix, then enter the JSON-RPC loop."""

    # ── Logger ──────────────────────────────────────────────────
    collector = LogCollector()
    nanopynix_util.install_logger(collector.callback)

    def _emit_events():
        for event in collector.drain():
            if event is None:
                continue
            req_id, action, *args = event
            _send(
                _notification(
                    "log",
                    {
                        "request_id": req_id,
                        "action": action,
                        "args": list(args),
                    },
                )
            )

    # ── Init ────────────────────────────────────────────────────
    # First message from parent is the init request (JSON-RPC)
    line = sys.stdin.readline()
    if not line:
        return
    init_msg = json.loads(line)

    store_uri = "auto"
    eval_store_uri = "auto"
    nix_conf = None
    settings = {}
    features: list[str] = []
    primops: list[dict[str, Any]] = []

    if init_msg.get("method") == "init":
        p = init_msg.get("params", {})
        store_uri = p.get("store_uri", store_uri)
        eval_store_uri = p.get("eval_store_uri", store_uri)
        nix_conf = p.get("nix_conf")
        settings = p.get("settings", {})
        features = p.get("experimental_features", [])
        primops = p.get("primops", [])
        req_id = init_msg.get("id")
    else:
        sys.stderr.write(f"worker: expected init, got {init_msg}\n")
        return

    try:
        # Apply config file path before init
        if nix_conf is not None:
            os.environ["NIX_USER_CONF_FILES"] = nix_conf
        if settings:
            os.environ["NIX_CONFIG"] = "\n".join(f"{k} = {v}" for k, v in settings.items())

        for k, v in settings.items():
            nanopynix_util.set_setting(k, v)
        for f in features:
            nanopynix_util.enable_experimental_feature(f)

        nanopynix_util.init_libstore(load_config=False)
        nanopynix_expr.init_libexpr()
        _register_primops(primops)

        store = nanopynix_store.open_store() if store_uri == "auto" else nanopynix_store.open_store(store_uri)

        eval_store = None
        if eval_store_uri != store_uri:
            eval_store = nanopynix_store.open_store(eval_store_uri)

        dispatch = {
            "store": _store_dispatch(store, eval_store),
            "eval": _eval_dispatch(store),
        }
    except Exception as exc:
        _send(
            _error(
                req_id,
                -32000,
                str(exc),
                {
                    "error_type": type(exc).__qualname__,
                    "traceback": traceback.format_exc(),
                },
            )
        )
        traceback.print_exc(file=sys.stderr)
        return

    _send(_result(req_id, "ok"))

    # ── RPC loop ────────────────────────────────────────────────
    for line in sys.stdin:
        msg = json.loads(line)
        method = msg.get("method", "")
        rid = msg.get("id")
        params = msg.get("params", [])

        parts = method.split(".", 1)
        if len(parts) != 2:
            _send(_error(rid, -32601, f"Invalid method: {method}"))
            continue

        ns, fn = parts
        ns_dispatch = dispatch.get(ns)
        if ns_dispatch is None:
            _send(_error(rid, -32601, f"Unknown namespace: {ns}"))
            continue

        handler = ns_dispatch.get(fn)
        if handler is None:
            _send(_error(rid, -32601, f"Unknown method: {method}"))
            continue

        nanopynix_util.set_logger_request_id(rid)
        try:
            value = handler(params)
            response = _result(rid, value)
        except Exception as exc:
            response = _error(
                rid,
                -32000,
                str(exc),
                {
                    "error_type": type(exc).__qualname__,
                    "traceback": traceback.format_exc(),
                },
            )
            traceback.print_exc(file=sys.stderr)
        finally:
            nanopynix_util.set_logger_request_id(0)
            _emit_events()

        _send(response)

    nanopynix_util.remove_logger()
    collector.close()


# ── Store dispatch ─────────────────────────────────────────────────────


def _store_dispatch(store, eval_store):
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

    return _dispatch(
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


# ── Eval dispatch ──────────────────────────────────────────────────────


_es: nanopynix_expr.EvalState | None = None


def _get_es(store):
    global _es
    if _es is None:
        _es = nanopynix_expr.EvalState(store)
    return _es


def _reset_es():
    """Release all handles and destroy the EvalState for a fresh session."""
    global _es
    if _es is not None:
        _es.release_all_exported()
        _es = None


def _eval_dispatch(store):
    """Return dispatch dict for eval operations."""

    def _remote_value(pyv) -> RemoteValueRef:
        return RemoteValueRef(value=ValueHandle.model_validate(_export(pyv)))

    def _deep_value(pyv):
        pyv.force()
        typ = pyv.type_name()
        if typ == "attrs":
            return DeepAttrs(attrs={name: _deep_value(pyv.attr_get(name)) for name in pyv.attr_names()})
        if typ == "list":
            return DeepList(items=[_deep_value(pyv.list_get(idx)) for idx in range(pyv.list_length())])
        if typ == "function":
            return _remote_value(pyv)
        if typ in {"null", "int", "float", "bool", "string", "path"}:
            return DeepScalar(value=pyv.to_python())
        msg = f"cannot forceDeep unsupported Nix value type '{typ}' over RPC"
        raise TypeError(msg)

    def _force_handle(handle: int):
        value = _get_es(store).value_from_handle(handle)
        value.force()
        if value.type_name() == "function":
            return _remote_value(value)
        return value.to_python()

    def _type_name(handle: int):
        value = _get_es(store).value_from_handle(handle)
        value.force()
        return value.type_name()

    def _export(pyv):
        es = _get_es(store)
        h = cast("Any", es)._export_pyvalue(pyv)
        return ValueHandle(handle=h, type=pyv.type_name()).model_dump(mode="json")

    def _flake_ref(ref):
        if isinstance(ref, str):
            return nanopynix_flake.parse_flake_ref(ref)
        msg = "flake references over RPC must currently be strings"
        raise TypeError(msg)

    def call(req: rpc.Call):
        es = _get_es(store)
        fn = es.value_from_handle(req.handle)

        def _call_arg_to_python(arg: rpc.CallArgWire):
            if isinstance(arg, RemoteCallArg):
                return es.value_from_handle(arg.handle)
            if isinstance(arg, ScalarCallArg):
                return arg.value
            if isinstance(arg, ListCallArg):
                return [_call_arg_to_python(item) for item in arg.items]
            if isinstance(arg, AttrsCallArg):
                return {key: _call_arg_to_python(item) for key, item in arg.attrs.items()}
            raise TypeError(f"unsupported call argument: {arg!r}")

        result = fn
        for arg in req.args:
            result = result.call(es.value_from_python(_call_arg_to_python(arg)))
        return _export(result)

    def eval_file(req: rpc.EvalFile):
        return _export(_get_es(store).eval_file(req.path))

    def eval_string(req: rpc.EvalString):
        return _export(_get_es(store).eval_string(req.expr, req.source_name))

    def force(req: rpc.Force):
        return _force_handle(req.handle)

    def force_deep(req: rpc.ForceDeep):
        value = _get_es(store).value_from_handle(req.handle)
        value.force_deep()
        return _deep_value(value)

    def attr(req: rpc.Attr):
        return _export(_get_es(store).value_from_handle(req.handle).attr_get(req.name))

    def list_get(req: rpc.ListGet):
        return _export(_get_es(store).value_from_handle(req.handle).list_get(req.index))

    def list_length(req: rpc.ListLength):
        return _get_es(store).value_from_handle(req.handle).list_length()

    def attr_names(req: rpc.AttrNames):
        return _get_es(store).value_from_handle(req.handle).attr_names()

    def has_attr(req: rpc.HasAttr):
        return _get_es(store).value_from_handle(req.handle).has_attr(req.name)

    def type_name(req: rpc.TypeName):
        return _type_name(req.handle)

    def lock_flake(req: rpc.LockFlake):
        return _locked_flake(nanopynix_flake.lock_flake(_get_es(store), _flake_ref(req.ref)))

    def get_flake(req: rpc.GetFlake) -> FlakeRef:
        return FlakeRef(attrs=_flake_ref_attrs(nanopynix_flake.get_flake(_get_es(store), _flake_ref(req.ref))))

    def release(req: rpc.Release):
        return _get_es(store).release_exported(req.handle)

    return _dispatch(
        [
            Endpoint(rpc.EvalFile, eval_file),
            Endpoint(rpc.EvalString, eval_string),
            Endpoint(rpc.Force, force),
            Endpoint(rpc.ForceDeep, force_deep),
            Endpoint(rpc.Attr, attr),
            Endpoint(rpc.ListGet, list_get),
            Endpoint(rpc.ListLength, list_length),
            Endpoint(rpc.AttrNames, attr_names),
            Endpoint(rpc.HasAttr, has_attr),
            Endpoint(rpc.TypeName, type_name),
            Endpoint(rpc.Call, call),
            Endpoint(rpc.LockFlake, lock_flake),
            Endpoint(rpc.GetFlake, get_flake),
            Endpoint(rpc.Release, release),
            Endpoint(rpc.ReleaseAll, lambda _: _reset_es()),
        ]
    )


if __name__ == "__main__":
    main()
