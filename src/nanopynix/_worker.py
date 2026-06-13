"""Subprocess worker — runs Nix in isolation via multiprocessing Pipes.

Spawned by ``WorkerPool`` with the ``forkserver`` start method.
Receives calls on ``req_conn``, sends results and log events on ``resp_conn``.
"""

from __future__ import annotations

import sys
import traceback

import nanopynix_expr
import nanopynix_store
import nanopynix_util

from nanopynix._extract import (
    build_result,
    missing_info,
    path_info,
    store_path,
    store_path_str,
)
from nanopynix.logging import LogCollector


def main(req_conn, resp_conn) -> None:
    """Bootstrap Nix, then enter the RPC loop."""
    # ── Logger ──────────────────────────────────────────────────
    collector = LogCollector()
    nanopynix_util.install_logger(collector.callback)

    def _emit_events():
        for event in collector.drain():
            if event is None:
                continue
            req_id, action, *args = event
            resp_conn.send({
                "type": "event",
                "id": req_id,
                "action": action,
                "args": list(args),
            })

    # ── Init ────────────────────────────────────────────────────
    init_msg = req_conn.recv()
    if init_msg.get("type") != "init":
        sys.stderr.write(f"worker: expected init message, got {init_msg}\n")
        return

    store_uri = init_msg.get("store_uri", "auto")
    eval_store_uri = init_msg.get("eval_store_uri", store_uri)
    settings = init_msg.get("settings", {})
    features = init_msg.get("experimental_features", [])

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

    resp_conn.send({"type": "ready"})

    # ── Dispatch table ──────────────────────────────────────────
    DISPATCH = {
        "store": _store_dispatch(store),
        "eval": _eval_dispatch(store),
    }

    # ── RPC loop ────────────────────────────────────────────────
    while True:
        msg = req_conn.recv()
        msg_type = msg.get("type")

        if msg_type == "close":
            break

        if msg_type != "call":
            sys.stderr.write(f"worker: unexpected message type: {msg_type}\n")
            continue

        req_id = msg["id"]
        module = msg["module"]
        fn = msg["fn"]
        args = msg.get("args", [])

        nanopynix_util.set_logger_request_id(req_id)
        try:
            handler = DISPATCH[module][fn]
            value = handler(args)
            response = {"type": "result", "id": req_id, "value": value}
        except Exception as exc:
            response = {
                "type": "error",
                "id": req_id,
                "msg": f"{type(exc).__name__}: {exc}",
            }
            traceback.print_exc(file=sys.stderr)
        finally:
            nanopynix_util.set_logger_request_id(0)
            _emit_events()

        resp_conn.send(response)

    nanopynix_util.remove_logger()
    collector.close()


# ── Dispatch helpers ────────────────────────────────────────────────

def _store_dispatch(store):
    """Return dispatch dict for store operations."""

    def _parse_sp(args):
        path = args[0]
        if not path.startswith("/"):
            path = f"{store.get_store_dir()}/{path}"
        sp = store.parse_store_path(path)
        return sp

    return {
        "get_uri": lambda _: store.get_uri(),
        "get_store_dir": lambda _: store.get_store_dir(),
        "is_valid_path": lambda args: store.is_valid_path(_parse_sp(args)),
        "parse_store_path": lambda args: store_path(_parse_sp(args)),
        "query_path_info": lambda args: path_info(store.query_path_info(_parse_sp(args))),
        "query_path_from_hash_part": lambda args: store_path(
            store.query_path_from_hash_part(args[0])
        ),
        "compute_fs_closure": lambda args: [
            store_path_str(s)
            for s in store.compute_fs_closure(
                _parse_sp(args),
                args[1] if len(args) > 1 else False,
                args[2] if len(args) > 2 else False,
                args[3] if len(args) > 3 else False,
            )
        ],
        "query_missing": lambda args: missing_info(
            store.query_missing([_parse_sp([p]) for p in args[0]])
        ),
        "query_derivation_outputs": lambda args: [
            store_path_str(s)
            for s in store.query_derivation_outputs(_parse_sp(args))
        ],
        "query_valid_derivers": lambda args: [
            store_path_str(s)
            for s in store.query_valid_derivers(_parse_sp(args))
        ],
        "query_all_valid_paths": lambda _: [
            store_path_str(s) for s in store.query_all_valid_paths()
        ],
        "query_referrers": lambda args: [
            store_path_str(s)
            for s in store.query_referrers(_parse_sp(args))
        ],
        "query_substitutable_paths": lambda args: [
            store_path_str(s)
            for s in store.query_substitutable_paths(
                [_parse_sp([p]) for p in args[0]]
            )
        ],
        "build_paths_with_results": lambda args: [
            build_result(r)
            for r in store.build_paths_with_results(
                [_parse_sp([p]) for p in args[0]],
                eval_store,
            )
        ],
        "read_derivation": lambda args: dict(
            store.read_derivation(_parse_sp(args))
        ),
        "build_derivation": lambda args: build_result(
            store.build_derivation(
                _parse_sp(args),
                nanopynix_store.BuildMode(args[1]) if len(args) > 1 else nanopynix_store.BuildMode.Normal,
            )
        ),
        "follow_links_to_store_path": lambda args: store_path(
            store.follow_links_to_store_path(args[0])
        ),
        "add_temp_root": lambda args: store.add_temp_root(_parse_sp(args)),
    }


# ── Eval dispatch ────────────────────────────────────────────────────

_es: "nanopynix_expr.EvalState | None" = None


def _get_es(store):
    global _es
    if _es is None:
        _es = nanopynix_expr.EvalState(store)
    return _es


def _eval_dispatch(store):
    """Return dispatch dict for eval operations."""

    def _force_handle(h):
        return _get_es(store).value_from_handle(h).to_python()

    def _export(pyv):
        es = _get_es(store)
        h = es._export_pyvalue(pyv)
        return {"handle": h, "type": pyv.type_name()}

    return {
        "eval_file":   lambda a: _export(_get_es(store).eval_file(a[0])),
        "eval_string": lambda a: _export(_get_es(store).eval_string(a[0], a[1] if len(a) > 1 else "<string>")),
        "force":       lambda a: _force_handle(a[0]),
        "attr":        lambda a: _export(_get_es(store).value_from_handle(a[0]).attr_get(a[1])),
        "list_get":    lambda a: _export(_get_es(store).value_from_handle(a[0]).list_get(a[1])),
        "list_length": lambda a: _get_es(store).value_from_handle(a[0]).list_length(),
        "attr_names":  lambda a: _get_es(store).value_from_handle(a[0]).attr_names(),
        "has_attr":    lambda a: _get_es(store).value_from_handle(a[0]).has_attr(a[1]),
        "type_name":   lambda a: _get_es(store).value_from_handle(a[0]).type_name(),
        "release":     lambda a: _get_es(store).release_exported(a[0]),
        "release_all": lambda _: _get_es(store).release_all_exported(),
    }
