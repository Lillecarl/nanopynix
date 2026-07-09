"""Eval and flake RPC dispatch for the worker subprocess."""

from __future__ import annotations

from typing import Any, cast

import nanopynix_expr
import nanopynix_flake
from nanopynix import _protocol as rpc
from nanopynix._extract import flake_ref_attrs as _flake_ref_attrs
from nanopynix._extract import locked_flake as _locked_flake
from nanopynix._worker_common import Endpoint, dispatch
from nanopynix.models import (
    AttrsCallArg,
    DeepAttrs,
    DeepList,
    DeepScalar,
    FlakeRef,
    ListCallArg,
    RemoteCallArg,
    RemoteValueRef,
    ScalarCallArg,
    ValueHandle,
)

_es: nanopynix_expr.EvalState | None = None

_locked_flakes: dict[int, nanopynix_flake.LockedFlake] = {}
_next_lf_handle: int = 1


def _get_es(store):
    global _es
    if _es is None:
        _es = nanopynix_expr.EvalState(store)
    return _es


def _reset_es():
    """Release eval and locked-flake handles for a fresh session."""
    global _es, _locked_flakes, _next_lf_handle
    if _es is not None:
        _es.release_all_exported()
        _es = None
    _locked_flakes.clear()
    _next_lf_handle = 1


def _flake_ref(ref):
    if isinstance(ref, str):
        return nanopynix_flake.parse_flake_ref(ref)
    msg = "flake references over RPC must currently be strings"
    raise TypeError(msg)


def eval_dispatch(store):
    """Return dispatch dict for eval operations."""

    def _export(pyv):
        es = _get_es(store)
        h = cast("Any", es)._export_pyvalue(pyv)
        return ValueHandle(handle=h, type=pyv.type_name()).model_dump(mode="json")

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

    def force_json(req: rpc.ForceJson):
        value = _get_es(store).value_from_handle(req.handle)
        return value.to_json(copy_to_store=req.copy_to_store)

    def attr(req: rpc.Attr):
        return _export(_get_es(store).value_from_handle(req.handle).attr_get(req.name))

    def list_get(req: rpc.ListGet):
        if req.index < 0:
            raise IndexError(f"list index must be non-negative, got {req.index}")
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
        global _next_lf_handle
        ref = nanopynix_flake.parse_flake_ref(req.ref)
        lf = nanopynix_flake.lock_flake(
            _get_es(store),
            ref,
            update_inputs=req.update_inputs,
            write_lock_file=req.write_lock_file,
        )
        handle = _next_lf_handle
        _next_lf_handle += 1
        _locked_flakes[handle] = lf
        result = _locked_flake(lf)
        result["handle"] = handle
        return result

    def call_locked_flake(req: rpc.CallLockedFlake):
        lf = _locked_flakes.get(req.handle)
        if lf is None:
            raise KeyError(f"locked flake handle {req.handle} not found")
        pyv = nanopynix_flake.call_flake(_get_es(store), lf)
        return _export(pyv)

    def write_lock_file(req: rpc.WriteLockFile):
        lf = _locked_flakes.get(req.handle)
        if lf is None:
            raise KeyError(f"locked flake handle {req.handle} not found")
        lf.write_lock_file()

    def release_locked_flake(req: rpc.ReleaseLockedFlake):
        _locked_flakes.pop(req.handle, None)

    def eval_flake(req: rpc.EvalFlake):
        pyv = nanopynix_flake.eval_flake(_get_es(store), req.ref, req.write_lock_file)
        return _export(pyv)

    def get_flake(req: rpc.GetFlake) -> FlakeRef:
        return FlakeRef(attrs=_flake_ref_attrs(nanopynix_flake.get_flake(_get_es(store), _flake_ref(req.ref))))

    def release(req: rpc.Release):
        return _get_es(store).release_exported(req.handle)

    return dispatch(
        [
            Endpoint(rpc.EvalFile, eval_file),
            Endpoint(rpc.EvalString, eval_string),
            Endpoint(rpc.Force, force),
            Endpoint(rpc.ForceDeep, force_deep),
            Endpoint(rpc.ForceJson, force_json),
            Endpoint(rpc.Attr, attr),
            Endpoint(rpc.ListGet, list_get),
            Endpoint(rpc.ListLength, list_length),
            Endpoint(rpc.AttrNames, attr_names),
            Endpoint(rpc.HasAttr, has_attr),
            Endpoint(rpc.TypeName, type_name),
            Endpoint(rpc.Call, call),
            Endpoint(rpc.LockFlake, lock_flake),
            Endpoint(rpc.CallLockedFlake, call_locked_flake),
            Endpoint(rpc.WriteLockFile, write_lock_file),
            Endpoint(rpc.ReleaseLockedFlake, release_locked_flake),
            Endpoint(rpc.EvalFlake, eval_flake),
            Endpoint(rpc.GetFlake, get_flake),
            Endpoint(rpc.Release, release),
            Endpoint(rpc.ReleaseAll, lambda _: _reset_es()),
        ]
    )
