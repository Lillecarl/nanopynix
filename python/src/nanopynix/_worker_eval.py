"""gRPC EvalService handler for the worker subprocess."""

from __future__ import annotations

from typing import Any, cast

import nanopynix_expr
import nanopynix_flake
from nanopynix._extract import (
    flake_ref_attrs as _flake_ref_attrs,
    locked_flake as _locked_flake,
)
from nanopynix._grpc_util import wrap_service_handlers
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
            if self._state.store is None:
                raise RuntimeError("store not initialized")
            self._state.eval_state = nanopynix_expr.EvalState(self._state.store)
        return self._state.eval_state

    def _reset(self) -> None:
        """Release eval and locked-flake handles for a fresh session."""
        es = self._state.eval_state
        if es is not None:
            es.release_all_exported()
            self._state.eval_state = None
        self._state.locked_flakes.clear()
        self._state._next_lf_handle = 1

    # ── value export helpers ──────────────────────────────────────

    def _export(self, pyv: Any) -> common_pb.ValueHandle:
        es = self._get_es()
        h = cast("Any", es)._export_pyvalue(pyv)
        type_name = pyv.type_name()
        nix_type = _NIX_TYPE_MAP.get(type_name, common_pb.NixType.UNSPECIFIED)
        return common_pb.ValueHandle(handle=h, type=nix_type)

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
        value = self._get_es().value_from_handle(handle)
        value.force()
        if value.type_name() == "function":
            return common_pb.ForceValue(remote_value=self._export(value))
        return common_pb.ForceValue(scalar=_pyval_to_scalar(value.to_python()))

    @staticmethod
    def _call_arg_to_python(arg: common_pb.CallArg, es: Any) -> Any:
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
            return [EvalServiceHandler._call_arg_to_python(item, es) for item in arg.list.items]
        if arg.attrs is not None:
            return {
                key: EvalServiceHandler._call_arg_to_python(val, es)
                for key, val in arg.attrs.entries.items()
            }
        if arg.remote_value is not None:
            return es.value_from_handle(arg.remote_value.handle)
        raise TypeError(f"unsupported call argument: {arg!r}")

    # ── eval methods ──────────────────────────────────────────────

    async def eval_file(self, message: EvalFileRequest) -> common_pb.ValueHandle:
        return self._export(self._get_es().eval_file(message.path))

    async def eval_string(self, message: EvalStringRequest) -> common_pb.ValueHandle:
        return self._export(self._get_es().eval_string(message.expr, message.source_name))

    async def force(self, message: ForceRequest) -> common_pb.ForceValue:
        return self._force_handle(message.handle)

    async def force_deep(self, message: ForceDeepRequest) -> common_pb.DeepValue:
        value = self._get_es().value_from_handle(message.handle)
        value.force_deep()
        return self._deep_value(value)

    async def force_json(self, message: ForceJsonRequest) -> ForceJsonResponse:
        value = self._get_es().value_from_handle(message.handle)
        return ForceJsonResponse(json=value.to_json(copy_to_store=message.copy_to_store))

    async def attr(self, message: AttrRequest) -> common_pb.ValueHandle:
        return self._export(self._get_es().value_from_handle(message.handle).attr_get(message.name))

    async def list_get(self, message: ListGetRequest) -> common_pb.ValueHandle:
        if message.index < 0:
            raise IndexError(f"list index must be non-negative, got {message.index}")
        return self._export(
            self._get_es().value_from_handle(message.handle).list_get(message.index)
        )

    async def list_length(self, message: ListLengthRequest) -> ListLengthResponse:
        return ListLengthResponse(
            length=self._get_es().value_from_handle(message.handle).list_length()
        )

    async def attr_names(self, message: AttrNamesRequest) -> AttrNamesResponse:
        return AttrNamesResponse(
            names=self._get_es().value_from_handle(message.handle).attr_names()
        )

    async def has_attr(self, message: HasAttrRequest) -> HasAttrResponse:
        return HasAttrResponse(
            has=self._get_es().value_from_handle(message.handle).has_attr(message.name)
        )

    async def type_name(self, message: TypeNameRequest) -> TypeNameResponse:
        value = self._get_es().value_from_handle(message.handle)
        value.force()
        type_name = value.type_name()
        nix_type = _NIX_TYPE_MAP.get(type_name, common_pb.NixType.UNSPECIFIED)
        return TypeNameResponse(**{"type": nix_type})

    async def call(self, message: CallRequest) -> common_pb.ValueHandle:
        es = self._get_es()
        fn = es.value_from_handle(message.handle)
        result = fn
        for arg in message.args:
            result = result.call(es.value_from_python(self._call_arg_to_python(arg, es)))
        return self._export(result)

    # ── flake methods ─────────────────────────────────────────────

    async def lock_flake(self, message: LockFlakeRequest) -> common_pb.LockedFlake:
        ref = nanopynix_flake.parse_flake_ref(message.ref)

        if message.update_all is not None:
            update_inputs: bool | list[str] = message.update_all
        elif message.update_inputs_list is not None:
            update_inputs = list(message.update_inputs_list.inputs)
        else:
            update_inputs = False

        lf = nanopynix_flake.lock_flake(
            self._get_es(),
            ref,
            update_inputs=update_inputs,
            write_lock_file=message.write_lock_file,
        )
        handle = self._state._next_lf_handle
        self._state._next_lf_handle += 1
        self._state.locked_flakes[handle] = lf

        lf_pb = _locked_flake(lf)
        lf_pb.handle = handle
        return lf_pb

    async def call_locked_flake(self, message: CallLockedFlakeRequest) -> common_pb.ValueHandle:
        lf = self._state.locked_flakes.get(message.handle)
        if lf is None:
            raise KeyError(f"locked flake handle {message.handle} not found")
        pyv = nanopynix_flake.call_flake(self._get_es(), lf)
        return self._export(pyv)

    async def write_lock_file(self, message: WriteLockFileRequest) -> WriteLockFileResponse:
        lf = self._state.locked_flakes.get(message.handle)
        if lf is None:
            raise KeyError(f"locked flake handle {message.handle} not found")
        lf.write_lock_file()
        return WriteLockFileResponse()

    async def release_locked_flake(
        self, message: ReleaseLockedFlakeRequest
    ) -> ReleaseLockedFlakeResponse:
        self._state.locked_flakes.pop(message.handle, None)
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
        self._get_es().release_exported(message.handle)
        return ReleaseResponse()

    async def release_all(self, message: ReleaseAllRequest) -> ReleaseAllResponse:
        self._reset()
        return ReleaseAllResponse()
