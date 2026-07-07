"""Subprocess worker — Nix execution over stdin/stdout JSON-RPC 2.0.

Spawned by ``Session._WorkerManager`` via ``asyncio.create_subprocess_exec``.
Reads JSON-RPC requests from stdin, writes responses and log-event
notifications to stdout.  One line per message (compact JSON, no embedded
newlines).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, cast

import nanopynix_expr
import nanopynix_fetchers
import nanopynix_flake
import nanopynix_store
import nanopynix_util
from pydantic import TypeAdapter

from nanopynix._extract import (
    flake_ref_attrs as _flake_ref_attrs,
    input_attrs as _input_attrs,
    locked_flake as _locked_flake,
    store_path as _sp_to_dict,
)
from nanopynix.logging import LogCollector
from nanopynix.models import BuildResult, Derivation, FlakeRef, Input, LockedFlake, MissingInfo, PathInfo, StorePath

_StorePathList = TypeAdapter(list[StorePath])
_BuildResultList = TypeAdapter(list[BuildResult])


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
            _send(_notification("log", {
                "request_id": req_id,
                "action": action,
                "args": list(args),
            }))

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

    if init_msg.get("method") == "init":
        p = init_msg.get("params", {})
        store_uri = p.get("store_uri", store_uri)
        eval_store_uri = p.get("eval_store_uri", store_uri)
        nix_conf = p.get("nix_conf")
        settings = p.get("settings", {})
        features = p.get("experimental_features", [])
        req_id = init_msg.get("id")
    else:
        sys.stderr.write(f"worker: expected init, got {init_msg}\n")
        return

    # Apply config file path before init
    if nix_conf is not None:
        os.environ["NIX_USER_CONF_FILES"] = nix_conf
    if settings:
        os.environ["NIX_CONFIG"] = "\n".join(
            f"{k} = {v}" for k, v in settings.items()
        )

    for k, v in settings.items():
        nanopynix_util.set_setting(k, v)
    for f in features:
        nanopynix_util.enable_experimental_feature(f)

    nanopynix_util.init_libstore(load_config=False)
    nanopynix_expr.init_libexpr()

    if store_uri == "auto":
        store = nanopynix_store.open_store()
    else:
        store = nanopynix_store.open_store(store_uri)

    eval_store = None
    if eval_store_uri != store_uri:
        eval_store = nanopynix_store.open_store(eval_store_uri)

    dispatch = {
        "store": _store_dispatch(store, eval_store),
        "eval": _eval_dispatch(store),
    }

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
                rid, -32000, str(exc),
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

    def _parse_sp(args):
        path = args[0]
        if not path.startswith("/"):
            path = f"{store.get_store_dir()}/{path}"
        return store.parse_store_path(path)

    def _store_path_list(paths):
        paths_model = _StorePathList.validate_python([
            p if isinstance(p, dict) else _sp_to_dict(p)
            for p in paths
        ])
        return _StorePathList.dump_python(paths_model, mode="json")

    return {
        "get_uri": lambda _: store.get_uri(),
        "get_store_dir": lambda _: store.get_store_dir(),
        "is_valid_path": lambda a: store.is_valid_path(_parse_sp(a)),
        "parse_store_path": lambda a: StorePath.model_validate(_sp_to_dict(_parse_sp(a))).model_dump(mode="json"),
        "query_path_info": lambda a: PathInfo.model_validate(dict(store.query_path_info(_parse_sp(a)))).model_dump(mode="json"),
        "query_path_from_hash_part": lambda a: (
            StorePath.model_validate(_sp_to_dict(sp)).model_dump(mode="json")
            if (sp := store.query_path_from_hash_part(a[0])) is not None else None
        ),
        "compute_fs_closure": lambda a: _store_path_list(
            store.compute_fs_closure(
                _parse_sp(a),
                a[1] if len(a) > 1 else False,
                a[2] if len(a) > 2 else False,
                a[3] if len(a) > 3 else False,
            )
        ),
        "query_missing": lambda a: MissingInfo.model_validate(dict(
            store.query_missing([_parse_sp([p]) for p in a[0]])
        )).model_dump(mode="json"),
        "query_derivation_outputs": lambda a: _store_path_list(
            store.query_derivation_outputs(_parse_sp(a))
        ),
        "query_valid_derivers": lambda a: _store_path_list(
            store.query_valid_derivers(_parse_sp(a))
        ),
        "query_all_valid_paths": lambda _: _store_path_list(store.query_all_valid_paths()),
        "query_referrers": lambda a: _store_path_list(store.query_referrers(_parse_sp(a))),
        "query_substitutable_paths": lambda a: _store_path_list(
            store.query_substitutable_paths(
                [_parse_sp([p]) for p in a[0]]
            )
        ),
        "build_paths_with_results": lambda a: _BuildResultList.dump_python(_BuildResultList.validate_python(list(
            store.build_paths_with_results(
                [_parse_sp([p]) for p in a[0]],
                eval_store,
            )
        )), mode="json"),
        "read_derivation": lambda a: Derivation.model_validate(dict(store.read_derivation(_parse_sp(a)))).model_dump(mode="json"),
        "build_derivation": lambda a: BuildResult.model_validate(
            store.build_derivation(
                _parse_sp(a),
                nanopynix_store.BuildMode(a[1]) if len(a) > 1 else nanopynix_store.BuildMode.Normal,
            )
        ).model_dump(mode="json"),
        "follow_links_to_store_path": lambda a: StorePath.model_validate(_sp_to_dict(
            store.follow_links_to_store_path(a[0])
        )).model_dump(mode="json"),
        "add_temp_root": lambda a: store.add_temp_root(_parse_sp(a)),
        "fetch_from_url": lambda a: Input(attrs=_input_attrs(nanopynix_fetchers.input_from_url(a[0]))).model_dump(mode="json"),
        "fetch_from_attrs": lambda a: Input(attrs=_input_attrs(nanopynix_fetchers.input_from_attrs(a[0]))).model_dump(mode="json"),
    }


# ── Eval dispatch ──────────────────────────────────────────────────────


_es: "nanopynix_expr.EvalState | None" = None


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

    def _force_handle(h):
        return _get_es(store).value_from_handle(h).to_python()

    def _type_name(h):
        value = _get_es(store).value_from_handle(h)
        value.force()
        return value.type_name()

    def _export(pyv):
        es = _get_es(store)
        h = cast(Any, es)._export_pyvalue(pyv)
        return {"handle": h, "type": pyv.type_name()}

    def _flake_ref(ref):
        if isinstance(ref, str):
            return nanopynix_flake.parse_flake_ref(ref)
        msg = "flake references over RPC must currently be strings"
        raise TypeError(msg)

    def _call(args):
        es = _get_es(store)
        fn = es.value_from_handle(args[0])
        result = fn
        for arg in args[1]:
            py_arg = es.value_from_python(arg)
            result = result.call(py_arg)
        return _export(result)

    return {
        "eval_file":   lambda a: _export(_get_es(store).eval_file(a[0])),
        "eval_string": lambda a: _export(_get_es(store).eval_string(a[0], a[1] if len(a) > 1 else "<string>")),
        "force":       lambda a: _force_handle(a[0]),
        "force_deep":  lambda a: _get_es(store).value_from_handle(a[0]).to_python(),
        "attr":        lambda a: _export(_get_es(store).value_from_handle(a[0]).attr_get(a[1])),
        "list_get":    lambda a: _export(_get_es(store).value_from_handle(a[0]).list_get(a[1])),
        "list_length": lambda a: _get_es(store).value_from_handle(a[0]).list_length(),
        "attr_names":  lambda a: _get_es(store).value_from_handle(a[0]).attr_names(),
        "has_attr":    lambda a: _get_es(store).value_from_handle(a[0]).has_attr(a[1]),
        "type_name":   lambda a: _type_name(a[0]),
        "call":        _call,
        "lock_flake":  lambda a: LockedFlake.model_validate(
            _locked_flake(nanopynix_flake.lock_flake(_get_es(store), _flake_ref(a[0])))
        ).model_dump(mode="json"),
        "get_flake":   lambda a: FlakeRef(
            attrs=_flake_ref_attrs(nanopynix_flake.get_flake(_get_es(store), _flake_ref(a[0])))
        ).model_dump(mode="json"),
        "release":     lambda a: _get_es(store).release_exported(a[0]),
        "release_all": lambda _: _reset_es(),
    }


if __name__ == "__main__":
    main()
