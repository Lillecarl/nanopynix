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
from typing import TYPE_CHECKING, Any

import nanopynix_expr
import nanopynix_store
import nanopynix_util
from nanopynix._worker_eval import eval_dispatch
from nanopynix._worker_store import store_dispatch
from nanopynix.logging import LogCollector
from nanopynix.models import PrimOpSpec

if TYPE_CHECKING:
    from collections.abc import Callable


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
            "store": store_dispatch(store, eval_store),
            "eval": eval_dispatch(store),
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


if __name__ == "__main__":
    main()
