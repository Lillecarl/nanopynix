"""gRPC EvalService handler for the worker subprocess."""

from __future__ import annotations

from typing import Any

from nanopynix_proto.nix import common as common_pb
from nanopynix_proto.nix.eval import (
    AttrNamesRequest,
    AttrNamesResponse,
    AttrRequest,
    CallLockedFlakeRequest,
    CallRequest,
    EvalFileRequest,
    EvalFlakeRequest,
    EvalServiceBase,
    EvalStringRequest,
    ForceDeepRequest,
    ForceJsonRequest,
    ForceJsonResponse,
    ForceRequest,
    GetFlakeRequest,
    HasAttrRequest,
    HasAttrResponse,
    ListGetRequest,
    ListLengthRequest,
    ListLengthResponse,
    LockFlakeRequest,
    ReleaseAllRequest,
    ReleaseAllResponse,
    ReleaseLockedFlakeRequest,
    ReleaseLockedFlakeResponse,
    ReleaseRequest,
    ReleaseResponse,
    TypeNameRequest,
    TypeNameResponse,
    WriteLockFileRequest,
    WriteLockFileResponse,
)

import nanopynix_expr
import nanopynix_flake
from nanopynix._extract import (
    flake_ref_attrs as _flake_ref_attrs,
)
from nanopynix._extract import (
    locked_flake as _locked_flake,
)
from nanopynix._grpc_util import wrap_service_handlers

# ── NixType string → enum mapping ────────────────────────────────────

_NIX_TYPE_MAP: dict[str, common_pb.NixType] = {
    "thunk": common_pb.NixType.THUNK,
    "int": common_pb.NixType.INT,
    "float": common_pb.NixType.FLOAT,
    "bool": common_pb.NixType.BOOL,
    "string": common_pb.NixType.STRING,
    "path": common_pb.NixType.PATH,
    "null": common_pb.NixType.NULL,
    "attrs": common_pb.NixType.ATTRS,
    "list": common_pb.NixType.LIST,
    "function": common_pb.NixType.FUNCTION,
    "external": common_pb.NixType.EXTERNAL,
    "unknown": common_pb.NixType.UNKNOWN,
}

_FORCE_SCALAR_TYPES = frozenset({"null", "int", "float", "bool", "string", "path"})
# ── scalar conversion helpers ────────────────────────────────────────
def _pyval_to_scalar(v: Any) -> common_pb.ScalarValue:
    """Convert a Python JSON-scalar value to a ScalarValue proto message."""
    if v is None:
        return common_pb.ScalarValue(null_value=common_pb.NullValue())
    if isinstance(v, bool):
        return common_pb.ScalarValue(bool_value=v)
    if isinstance(v, int):
        return common_pb.ScalarValue(int_value=v)
    if isinstance(v, float):
        return common_pb.ScalarValue(float_value=v)
    return common_pb.ScalarValue(string_value=str(v))
# ── Service handler ──────────────────────────────────────────────────
@wrap_service_handlers
class EvalServiceHandler(EvalServiceBase):
    """gRPC handler for all eval/flake operations."""

    def __init__(self, state: Any) -> None:
        self._state = state

    # ── eval state management ─────────────────────────────────────

    def _get_es(self) -> Any:
        if self._state.eval_state is None:
            stores = self._state.handles.iter_kind("store")
            if not stores:
                raise RuntimeError("no store open — open a store before evaluating")
            self._state.eval_state = nanopynix_expr.EvalState(stores[0][1])
        return self._state.eval_state

    def _reset(self) -> None:
        """Release value and locked-flake handles for a fresh session."""
        es = self._state.eval_state
        if es is not None:
            for handle, resource in self._state.handles.iter_kind("value"):
                es.release_exported_value(resource)
                self._state.handles.release(handle)
            self._state.eval_state = None
        for handle, _resource in self._state.handles.iter_kind("locked_flake"):
            self._state.handles.release(handle)

    # ── value export helpers ──────────────────────────────────────

    def _export(self, pyv: Any) -> common_pb.ValueHandle:
        es = self._get_es()
        exported = es._export_pyvalue(pyv)
        handle = self._state.handles.allocate(exported, "value")
        type_name = pyv.type_name()
        nix_type = _NIX_TYPE_MAP.get(type_name, common_pb.NixType.UNSPECIFIED)
        return common_pb.ValueHandle(handle=handle, type=nix_type)

    def _resolve(self, handle: int) -> Any:
        return self._state.handles.get_typed(handle, "value")

    def _deep_value(self, pyv: Any) -> common_pb.DeepValue:
        pyv.force()
        typ = pyv.type_name()
        if typ == "attrs":
            return common_pb.DeepValue(
                attrs=common_pb.DeepAttrs(
                    entries={name: self._deep_value(pyv.attr_get(name)) for name in pyv.attr_names()}
                )
            )
        if typ == "list":
            return common_pb.DeepValue(
                list=common_pb.DeepList(
                    items=[self._deep_value(pyv.list_get(idx)) for idx in range(pyv.list_length())]
                )
            )
        if typ == "function":
            return common_pb.DeepValue(remote_value=self._export(pyv))
        if typ in _FORCE_SCALAR_TYPES:
            return common_pb.DeepValue(scalar=_pyval_to_scalar(pyv.to_python()))
        raise TypeError(f"cannot forceDeep unsupported Nix value type '{typ}' over RPC")

    def _force_handle(self, handle: int) -> common_pb.ForceValue:
        value = self._resolve(handle)
        value.force()
        if value.type_name() == "function":
            return common_pb.ForceValue(remote_value=self._export(value))
        return common_pb.ForceValue(scalar=_pyval_to_scalar(value.to_python()))

    def _call_arg_to_python(self, arg: common_pb.CallArg, es: Any) -> Any:
        """Convert a CallArg proto to a Python/nanobind value for Nix calls."""
        if arg.scalar is not None:
            sv = arg.scalar
            if sv.string_value is not None:
                return sv.string_value
            if sv.int_value is not None:
                return sv.int_value
            if sv.float_value is not None:
                return sv.float_value
            if sv.bool_value is not None:
                return sv.bool_value
            return None  # null_value
        if arg.list is not None:
            return [self._call_arg_to_python(item, es) for item in arg.list.items]
        if arg.attrs is not None:
            return {
                key: self._call_arg_to_python(val, es)
                for key, val in arg.attrs.entries.items()
            }
        if arg.remote_value is not None:
            return self._resolve(arg.remote_value.handle)
        raise TypeError(f"unsupported call argument: {arg!r}")

    # ── eval methods ──────────────────────────────────────────────

    async def eval_file(self, message: EvalFileRequest) -> common_pb.ValueHandle:
        return self._export(self._get_es().eval_file(message.path))

    async def eval_string(self, message: EvalStringRequest) -> common_pb.ValueHandle:
        return self._export(self._get_es().eval_string(message.expr, message.source_name))

    async def force(self, message: ForceRequest) -> common_pb.ForceValue:
        return self._force_handle(message.handle)

    async def force_deep(self, message: ForceDeepRequest) -> common_pb.DeepValue:
        value = self._resolve(message.handle)
        value.force_deep()
        return self._deep_value(value)

    async def force_json(self, message: ForceJsonRequest) -> ForceJsonResponse:
        import json as _json

        value = self._resolve(message.handle)
        return ForceJsonResponse(json=_json.dumps(value.to_json(copy_to_store=message.copy_to_store)))

    async def attr(self, message: AttrRequest) -> common_pb.ValueHandle:
        return self._export(self._resolve(message.handle).attr_get(message.name))

    async def list_get(self, message: ListGetRequest) -> common_pb.ValueHandle:
        if message.index < 0:
            raise IndexError(f"list index must be non-negative, got {message.index}")
        return self._export(
            self._resolve(message.handle).list_get(message.index)
        )

    async def list_length(self, message: ListLengthRequest) -> ListLengthResponse:
        return ListLengthResponse(
            length=self._resolve(message.handle).list_length()
        )

    async def attr_names(self, message: AttrNamesRequest) -> AttrNamesResponse:
        return AttrNamesResponse(
            names=self._resolve(message.handle).attr_names()
        )

    async def has_attr(self, message: HasAttrRequest) -> HasAttrResponse:
        return HasAttrResponse(
            has=self._resolve(message.handle).has_attr(message.name)
        )

    async def type_name(self, message: TypeNameRequest) -> TypeNameResponse:
        value = self._resolve(message.handle)
        value.force()
        type_name = value.type_name()
        nix_type = _NIX_TYPE_MAP.get(type_name, common_pb.NixType.UNSPECIFIED)
        return TypeNameResponse(type=nix_type)

    async def call(self, message: CallRequest) -> common_pb.ValueHandle:
        es = self._get_es()
        fn = self._resolve(message.handle)
        result = fn
        for arg in message.args:
            result = result.call(es.value_from_python(self._call_arg_to_python(arg, es)))
        return self._export(result)

    # ── flake methods ─────────────────────────────────────────────

    async def lock_flake(self, message: LockFlakeRequest) -> common_pb.LockedFlake:
        if self._state.collector is not None:
            self._state.collector.callback(0, "msg", 3, f"lock_flake: parsing ref '{message.ref}'")
        ref = nanopynix_flake.parse_flake_ref(message.ref)

        if message.update_all is not None:
            update_inputs: bool | list[str] = message.update_all
        elif message.update_inputs_list is not None:
            update_inputs = list(message.update_inputs_list.inputs)
        else:
            update_inputs = False

        if self._state.collector is not None:
            self._state.collector.callback(0, "msg", 3, f"lock_flake: calling C++ lock_flake write_lock_file={message.write_lock_file}")
        lf = nanopynix_flake.lock_flake(
            self._get_es(),
            ref,
            update_inputs=update_inputs,
            write_lock_file=message.write_lock_file,
        )
        if self._state.collector is not None:
            self._state.collector.callback(0, "msg", 3, "lock_flake: C++ lock_flake returned")
        handle = self._state.handles.allocate(lf, "locked_flake")

        lf_pb = _locked_flake(lf)
        lf_pb.handle = handle
        return lf_pb

    async def call_locked_flake(self, message: CallLockedFlakeRequest) -> common_pb.ValueHandle:
        lf = self._state.handles.get_typed(message.handle, "locked_flake")
        pyv = nanopynix_flake.call_flake(self._get_es(), lf)
        return self._export(pyv)

    async def write_lock_file(self, message: WriteLockFileRequest) -> WriteLockFileResponse:
        lf = self._state.handles.get_typed(message.handle, "locked_flake")
        lf.write_lock_file()
        return WriteLockFileResponse()

    async def release_locked_flake(
        self, message: ReleaseLockedFlakeRequest
    ) -> ReleaseLockedFlakeResponse:
        self._state.handles.release(message.handle)
        return ReleaseLockedFlakeResponse()

    async def eval_flake(self, message: EvalFlakeRequest) -> common_pb.ValueHandle:
        pyv = nanopynix_flake.eval_flake(
            self._get_es(), message.ref, message.write_lock_file
        )
        return self._export(pyv)

    async def get_flake(self, message: GetFlakeRequest) -> common_pb.FlakeRef:
        ref = nanopynix_flake.parse_flake_ref(message.ref)
        fr = nanopynix_flake.get_flake(self._get_es(), ref)
        return common_pb.FlakeRef(attrs=_flake_ref_attrs(fr))

    async def release(self, message: ReleaseRequest) -> ReleaseResponse:
        es = self._state.eval_state
        if es is not None:
            pyv = self._resolve(message.handle)
            es.release_exported_value(pyv)
        self._state.handles.release(message.handle)
        return ReleaseResponse()

    async def release_all(self, message: ReleaseAllRequest) -> ReleaseAllResponse:
        self._reset()
        return ReleaseAllResponse()
