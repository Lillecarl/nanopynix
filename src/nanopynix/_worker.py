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

from nanopynix._extract import store_path as _sp_to_dict
from nanopynix.logging import LogCollector


def _try_send(conn, msg):
    """Non-blocking send — returns True if sent, False if would block."""
    import select

    fd = conn.fileno()
    r, w, x = select.select([], [fd], [], 0)
    if w:
        conn.send(msg)
        return True
    return False


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
            _try_send(resp_conn, {
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
        "store": _store_dispatch(store, eval_store),
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
                "error_type": type(exc).__qualname__,
                "msg": str(exc),
                "traceback": traceback.format_exc(),
            }
            traceback.print_exc(file=sys.stderr)
        finally:
            nanopynix_util.set_logger_request_id(0)
            _emit_events()

        resp_conn.send(response)

    nanopynix_util.remove_logger()
    collector.close()


# ── Dispatch helpers ────────────────────────────────────────────────

def _store_dispatch(store, eval_store):
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
        # parse_store_path returns a C++ StorePath — convert to dict
        "parse_store_path": lambda args: _sp_to_dict(_parse_sp(args)),
        # query_path_info now returns nb::dict directly
        "query_path_info": lambda args: dict(store.query_path_info(_parse_sp(args))),
        "query_path_from_hash_part": lambda args: (
            _sp_to_dict(sp) if (sp := store.query_path_from_hash_part(args[0])) is not None else None
        ),
        # compute_fs_closure now returns list of dicts
        "compute_fs_closure": lambda args: list(
            store.compute_fs_closure(
                _parse_sp(args),
                args[1] if len(args) > 1 else False,
                args[2] if len(args) > 2 else False,
                args[3] if len(args) > 3 else False,
            )
        ),
        # query_missing returns nb::dict directly
        "query_missing": lambda args: dict(
            store.query_missing([_parse_sp([p]) for p in args[0]])
        ),
        # derivation outputs / valid derivers return list of dicts
        "query_derivation_outputs": lambda args: list(
            store.query_derivation_outputs(_parse_sp(args))
        ),
        "query_valid_derivers": lambda args: list(
            store.query_valid_derivers(_parse_sp(args))
        ),
        "query_all_valid_paths": lambda _: list(store.query_all_valid_paths()),
        "query_referrers": lambda args: list(store.query_referrers(_parse_sp(args))),
        "query_substitutable_paths": lambda args: list(
            store.query_substitutable_paths(
                [_parse_sp([p]) for p in args[0]]
            )
        ),
        # build_paths_with_results returns list of dicts
        "build_paths_with_results": lambda args: list(
            store.build_paths_with_results(
                [_parse_sp([p]) for p in args[0]],
                eval_store,
            )
        ),
        "read_derivation": lambda args: dict(store.read_derivation(_parse_sp(args))),
        # build_derivation returns nb::dict directly
        "build_derivation": lambda args: dict(
            store.build_derivation(
                _parse_sp(args),
                nanopynix_store.BuildMode(args[1]) if len(args) > 1 else nanopynix_store.BuildMode.Normal,
            )
        ),
        "follow_links_to_store_path": lambda args: _sp_to_dict(
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

    def _export(pyv):
        es = _get_es(store)
        h = es._export_pyvalue(pyv)
        return {"handle": h, "type": pyv.type_name()}

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
        "type_name":   lambda a: _get_es(store).value_from_handle(a[0]).type_name(),
        "release":     lambda a: _get_es(store).release_exported(a[0]),
        "release_all": lambda _: _reset_es(),
    }
