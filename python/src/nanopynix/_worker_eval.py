"""gRPC EvalService handler for the worker subprocess.

Thread boundary: all Nix C++ operations run on the dedicated Nix thread
(via ``NixThreadExecutor.run()``).  The gRPC handlers run on the event
loop and dispatch synchronously — the event loop never blocks on Nix.
"""

from __future__ import annotations

import json as _json
from typing import Any

from nanopynix_proto.nix import common as common_pb
from nanopynix_proto.nix.eval import (
    AttrNamesRequest,
    AttrNamesResponse,
    AttrRequest,
    AutoCallRequest,
    BeginReplRequest,
    BeginReplResponse,
    BuildRequest,
    BuildResponse,
    CallLockedFlakeRequest,
    CallRequest,
    EditLocationRequest,
    EditLocationResponse,
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
    RealiseArgvRequest,
    RealiseArgvResponse,
    RealiseStringRequest,
    RealiseStringResponse,
    ReleaseAllRequest,
    ReleaseAllResponse,
    ReleaseLockedFlakeRequest,
    ReleaseLockedFlakeResponse,
    ReleaseRequest,
    ReleaseResponse,
    ReplAddAttrsRequest,
    ReplAddAttrsResponse,
    ReplLoadFileRequest,
    ReplProcessLineRequest,
    ReplProcessLineResponse,
    ReplScopeNamesRequest,
    ReplScopeNamesResponse,
    ResetFileCacheRequest,
    ResetFileCacheResponse,
    TypeNameRequest,
    TypeNameResponse,
    WriteLockFileRequest,
    WriteLockFileResponse,
)

import nanopynix_flake
from nanopynix._extract import flake_ref_attrs as _flake_ref_attrs
from nanopynix._extract import locked_flake as _locked_flake
from nanopynix._grpc_util import wrap_service_handlers
from nanopynix._service_adapter import (
    _proto_shape,  # type: ignore[reportPrivateUsage] -- internal module registry pattern
)

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


def _pyval_to_scalar(v: Any) -> common_pb.ScalarValue:
    if v is None:
        return common_pb.ScalarValue(null_value=common_pb.NullValue())
    if isinstance(v, bool):
        return common_pb.ScalarValue(bool_value=v)
    if isinstance(v, int):
        return common_pb.ScalarValue(int_value=v)
    if isinstance(v, float):
        return common_pb.ScalarValue(float_value=v)
    return common_pb.ScalarValue(string_value=str(v))


@wrap_service_handlers
class EvalServiceHandler(EvalServiceBase):
    """gRPC handler for all eval/flake operations.

    Every method follows the same dispatch pattern::

        async def method(self, message):
            return await self._state.executor.run(self._do_method, message)
    """

    def __init__(self, state: Any) -> None:
        self._state = state

    # ── helpers (run on the Nix thread) ──────────────────────────

    def _get_es(self, store_handle: int | None = None) -> Any:
        if store_handle is not None:
            self._select_store_handle(store_handle)
        if self._state.eval_state is None:
            raise RuntimeError("no eval store selected — open a store before evaluating")
        return self._state.eval_state

    def _select_store_handle(self, store_handle: int) -> None:
        store = self._state.handles.get_typed(store_handle, "store")
        if self._state.eval_state is not None and self._state.eval_store_handle == store_handle:
            return
        if self._state.eval_state is not None:
            raise RuntimeError("eval session is already bound to a different store")
        self._state.eval_state = self._state.runtime.open_eval_state(store, self._state.nix_path)
        self._state.eval_store_handle = store_handle

    def _reset(self) -> None:
        es = self._state.eval_state
        if es is not None:
            for handle, _resource in self._state.handles.iter_kind("value"):
                self._state.handles.release(handle)
            self._state.eval_state = None
            self._state.eval_store_handle = None
        for handle, _resource in self._state.handles.iter_kind("locked_flake"):
            self._state.handles.release(handle)

    def _export(self, pyv: Any) -> common_pb.ValueHandle:
        self._get_es()
        handle = self._state.handles.allocate(pyv, "value")
        type_name = pyv.type_name()
        nix_type = _NIX_TYPE_MAP.get(type_name, common_pb.NixType.UNSPECIFIED)
        return common_pb.ValueHandle(handle=handle, type=nix_type)

    def _resolve(self, handle: int) -> Any:
        return self._state.handles.get_typed(handle, "value")

    def _get_store(self, store_handle: int) -> Any:
        return self._state.handles.get_typed(store_handle, "store")

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
                list=common_pb.DeepList(items=[self._deep_value(pyv.list_get(idx)) for idx in range(pyv.list_length())])
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
            return None
        if arg.list is not None:
            return [self._call_arg_to_python(item, es) for item in arg.list.items]
        if arg.attrs is not None:
            return {key: self._call_arg_to_python(val, es) for key, val in arg.attrs.entries.items()}
        if arg.remote_value is not None:
            return self._resolve(arg.remote_value.handle)
        raise TypeError(f"unsupported call argument: {arg!r}")

    # ── eval methods ──────────────────────────────────────────────

    async def eval_file(self, message: EvalFileRequest) -> common_pb.ValueHandle:
        return await self._state.executor.run(self._do_eval_file, message)

    def _do_eval_file(self, message: EvalFileRequest) -> common_pb.ValueHandle:
        es = self._get_es(message.store_handle).require_raw()
        value = es.repl_eval_file(message.path) if es.repl_active() else es.eval_file(message.path)
        return self._export(value)

    async def repl_load_file(self, message: ReplLoadFileRequest) -> common_pb.ValueHandle:
        return await self._state.executor.run(self._do_repl_load_file, message)

    def _do_repl_load_file(self, message: ReplLoadFileRequest) -> common_pb.ValueHandle:
        return self._export(self._get_es(message.store_handle).repl_load_file(message.path))

    async def eval_string(self, message: EvalStringRequest) -> common_pb.ValueHandle:
        return await self._state.executor.run(self._do_eval_string, message)

    def _do_eval_string(self, message: EvalStringRequest) -> common_pb.ValueHandle:
        es = self._get_es(message.store_handle).require_raw()
        value = es.repl_eval_string(message.expr, message.source_name) if es.repl_active() else es.eval_string(
            message.expr, message.source_name
        )
        return self._export(value)

    async def begin_repl(self, message: BeginReplRequest) -> BeginReplResponse:
        return await self._state.executor.run(self._do_begin_repl, message)

    def _do_begin_repl(self, message: BeginReplRequest) -> BeginReplResponse:
        self._get_es(message.store_handle).begin_repl()
        return BeginReplResponse()

    async def repl_process_line(self, message: ReplProcessLineRequest) -> ReplProcessLineResponse:
        return await self._state.executor.run(self._do_repl_process_line, message)

    def _do_repl_process_line(self, message: ReplProcessLineRequest) -> ReplProcessLineResponse:
        value = self._get_es(message.store_handle).repl_process_line(message.line, message.source_name)
        if value is None:
            return ReplProcessLineResponse(is_binding=True)
        return ReplProcessLineResponse(is_binding=False, value=self._export(value))

    async def repl_add_attrs(self, message: ReplAddAttrsRequest) -> ReplAddAttrsResponse:
        return await self._state.executor.run(self._do_repl_add_attrs, message)

    def _do_repl_add_attrs(self, message: ReplAddAttrsRequest) -> ReplAddAttrsResponse:
        return ReplAddAttrsResponse(names=self._get_es().repl_add_attrs(self._resolve(message.handle)))

    async def repl_scope_names(self, message: ReplScopeNamesRequest) -> ReplScopeNamesResponse:
        return await self._state.executor.run(self._do_repl_scope_names, message)

    def _do_repl_scope_names(self, message: ReplScopeNamesRequest) -> ReplScopeNamesResponse:
        return ReplScopeNamesResponse(names=self._get_es().repl_scope_names())

    async def reset_file_cache(self, message: ResetFileCacheRequest) -> ResetFileCacheResponse:
        return await self._state.executor.run(self._do_reset_file_cache, message)

    def _do_reset_file_cache(self, message: ResetFileCacheRequest) -> ResetFileCacheResponse:
        del message
        self._get_es().reset_file_cache()
        return ResetFileCacheResponse()

    async def force(self, message: ForceRequest) -> common_pb.ForceValue:
        return await self._state.executor.run(self._do_force, message)

    def _do_force(self, message: ForceRequest) -> common_pb.ForceValue:
        return self._force_handle(message.handle)

    async def force_deep(self, message: ForceDeepRequest) -> common_pb.DeepValue:
        return await self._state.executor.run(self._do_force_deep, message)

    def _do_force_deep(self, message: ForceDeepRequest) -> common_pb.DeepValue:
        value = self._resolve(message.handle)
        value.force_deep()
        return self._deep_value(value)

    async def force_json(self, message: ForceJsonRequest) -> ForceJsonResponse:
        return await self._state.executor.run(self._do_force_json, message)

    def _do_force_json(self, message: ForceJsonRequest) -> ForceJsonResponse:
        value = self._resolve(message.handle)
        return ForceJsonResponse(json=_json.dumps(value.to_json(copy_to_store=message.copy_to_store)))

    async def realise_string(self, message: RealiseStringRequest) -> RealiseStringResponse:
        return await self._state.executor.run(self._do_realise_string, message)

    def _do_realise_string(self, message: RealiseStringRequest) -> RealiseStringResponse:
        return RealiseStringResponse(value=self._resolve(message.handle).realise_string())

    async def realise_argv(self, message: RealiseArgvRequest) -> RealiseArgvResponse:
        return await self._state.executor.run(self._do_realise_argv, message)

    def _do_realise_argv(self, message: RealiseArgvRequest) -> RealiseArgvResponse:
        return RealiseArgvResponse(argv=self._resolve(message.handle).realise_argv())

    async def edit_location(self, message: EditLocationRequest) -> EditLocationResponse:
        return await self._state.executor.run(self._do_edit_location, message)

    def _do_edit_location(self, message: EditLocationRequest) -> EditLocationResponse:
        location = self._resolve(message.handle).edit_location()
        return EditLocationResponse(path=location["path"], line=location["line"])

    async def attr(self, message: AttrRequest) -> common_pb.ValueHandle:
        return await self._state.executor.run(self._do_attr, message)

    def _do_attr(self, message: AttrRequest) -> common_pb.ValueHandle:
        return self._export(self._resolve(message.handle).attr_get(message.name))

    async def list_get(self, message: ListGetRequest) -> common_pb.ValueHandle:
        return await self._state.executor.run(self._do_list_get, message)

    def _do_list_get(self, message: ListGetRequest) -> common_pb.ValueHandle:
        pv = self._resolve(message.handle)
        idx = message.index
        if idx < 0:
            idx += pv.list_length()
        if idx < 0:
            raise IndexError(f"list index out of range: {message.index}")
        return self._export(pv.list_get(idx))

    async def list_length(self, message: ListLengthRequest) -> ListLengthResponse:
        return await self._state.executor.run(self._do_list_length, message)

    def _do_list_length(self, message: ListLengthRequest) -> ListLengthResponse:
        return ListLengthResponse(length=self._resolve(message.handle).list_length())

    async def attr_names(self, message: AttrNamesRequest) -> AttrNamesResponse:
        return await self._state.executor.run(self._do_attr_names, message)

    def _do_attr_names(self, message: AttrNamesRequest) -> AttrNamesResponse:
        return AttrNamesResponse(names=self._resolve(message.handle).attr_names())

    async def has_attr(self, message: HasAttrRequest) -> HasAttrResponse:
        return await self._state.executor.run(self._do_has_attr, message)

    def _do_has_attr(self, message: HasAttrRequest) -> HasAttrResponse:
        return HasAttrResponse(has=self._resolve(message.handle).has_attr(message.name))

    async def type_name(self, message: TypeNameRequest) -> TypeNameResponse:
        return await self._state.executor.run(self._do_type_name, message)

    def _do_type_name(self, message: TypeNameRequest) -> TypeNameResponse:
        value = self._resolve(message.handle)
        value.force()
        type_name = value.type_name()
        nix_type = _NIX_TYPE_MAP.get(type_name, common_pb.NixType.UNSPECIFIED)
        return TypeNameResponse(type=nix_type)

    async def auto_call(self, message: AutoCallRequest) -> common_pb.ValueHandle:
        return await self._state.executor.run(self._do_auto_call, message)

    def _do_auto_call(self, message: AutoCallRequest) -> common_pb.ValueHandle:
        return self._export(self._resolve(message.handle).auto_call())

    async def call(self, message: CallRequest) -> common_pb.ValueHandle:
        return await self._state.executor.run(self._do_call, message)

    def _do_call(self, message: CallRequest) -> common_pb.ValueHandle:
        es = self._get_es()
        fn = self._resolve(message.handle)
        result = fn
        for arg in message.args:
            result = result.call(es.value_from_python(self._call_arg_to_python(arg, es)))
        return self._export(result)

    async def build(self, message: BuildRequest) -> BuildResponse:
        return await self._state.executor.run(self._do_build, message)

    def _do_build(self, message: BuildRequest) -> BuildResponse:
        self._get_es()
        value = self._resolve(message.handle)
        build_store = self._get_store(message.build_store_handle) if message.build_store_handle else None
        eval_store = None
        if build_store is not None and self._state.eval_store_handle != message.build_store_handle:
            if self._state.eval_store_handle is None:
                raise RuntimeError("no eval store selected — open a store before building")
            eval_store = self._get_store(self._state.eval_store_handle)
        if self._state.collector is not None:
            self._state.collector.callback(
                0,
                "msg",
                int(common_pb.LogLevel.DEBUG),
                "eval build start "
                f"handle={message.handle} build_store_handle={message.build_store_handle or 'eval'} "
                f"eval_store={'separate' if eval_store is not None else 'none'} build_mode={message.build_mode}",
            )
        raw = value.build(
            None if build_store is None else build_store.require_raw(),
            message.build_mode,
            None if eval_store is None else eval_store.require_raw(),
        )
        if self._state.collector is not None:
            shaped = _proto_shape(raw)
            self._state.collector.callback(
                0,
                "msg",
                int(common_pb.LogLevel.DEBUG),
                f"eval build finish drv_path={shaped.get('drv_path', '')} outputs={sorted(shaped.get('outputs', {}))}",
            )
            return BuildResponse.from_dict(shaped)
        return BuildResponse.from_dict(_proto_shape(raw))

    # ── flake methods ─────────────────────────────────────────────

    async def lock_flake(self, message: LockFlakeRequest) -> common_pb.LockedFlake:
        return await self._state.executor.run(self._do_lock_flake, message)

    def _do_lock_flake(self, message: LockFlakeRequest) -> common_pb.LockedFlake:
        if self._state.collector is not None:
            self._state.collector.callback(0, "msg", 3, f"lock_flake: parsing ref '{message.ref}'")
        es = self._get_es(message.store_handle).require_raw()
        ref = nanopynix_flake.parse_flake_ref(message.ref)

        if message.update_all is not None:
            update_inputs: bool | list[str] = message.update_all
        elif message.update_inputs_list is not None:
            update_inputs = list(message.update_inputs_list.inputs)
        else:
            update_inputs = False

        if self._state.collector is not None:
            self._state.collector.callback(
                0, "msg", 3, f"lock_flake: calling C++ lock_flake write_lock_file={message.write_lock_file}"
            )
        lf = nanopynix_flake.lock_flake(
            es,
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
        return await self._state.executor.run(self._do_call_locked_flake, message)

    def _do_call_locked_flake(self, message: CallLockedFlakeRequest) -> common_pb.ValueHandle:
        lf = self._state.handles.get_typed(message.handle, "locked_flake")
        pyv = nanopynix_flake.call_flake(self._get_es().require_raw(), lf)
        return self._export(pyv)

    async def write_lock_file(self, message: WriteLockFileRequest) -> WriteLockFileResponse:
        return await self._state.executor.run(self._do_write_lock_file, message)

    def _do_write_lock_file(self, message: WriteLockFileRequest) -> WriteLockFileResponse:
        lf = self._state.handles.get_typed(message.handle, "locked_flake")
        lf.write_lock_file()
        return WriteLockFileResponse()

    async def release_locked_flake(self, message: ReleaseLockedFlakeRequest) -> ReleaseLockedFlakeResponse:
        return await self._state.executor.run(self._do_release_locked_flake, message)

    def _do_release_locked_flake(self, message: ReleaseLockedFlakeRequest) -> ReleaseLockedFlakeResponse:
        self._state.handles.release(message.handle)
        return ReleaseLockedFlakeResponse()

    async def eval_flake(self, message: EvalFlakeRequest) -> common_pb.ValueHandle:
        return await self._state.executor.run(self._do_eval_flake, message)

    def _do_eval_flake(self, message: EvalFlakeRequest) -> common_pb.ValueHandle:
        pyv = nanopynix_flake.eval_flake(
            self._get_es(message.store_handle).require_raw(),
            message.ref,
            message.write_lock_file,
        )
        return self._export(pyv)

    async def get_flake(self, message: GetFlakeRequest) -> common_pb.FlakeRef:
        return await self._state.executor.run(self._do_get_flake, message)

    def _do_get_flake(self, message: GetFlakeRequest) -> common_pb.FlakeRef:
        ref = nanopynix_flake.parse_flake_ref(message.ref)
        fr = nanopynix_flake.get_flake(self._get_es(message.store_handle).require_raw(), ref)
        return common_pb.FlakeRef(attrs=_flake_ref_attrs(fr))

    async def release(self, message: ReleaseRequest) -> ReleaseResponse:
        return await self._state.executor.run(self._do_release, message)

    def _do_release(self, message: ReleaseRequest) -> ReleaseResponse:
        self._state.handles.release(message.handle)
        return ReleaseResponse()

    async def release_all(self, message: ReleaseAllRequest) -> ReleaseAllResponse:
        return await self._state.executor.run(self._do_release_all, message)

    def _do_release_all(self, message: ReleaseAllRequest) -> ReleaseAllResponse:
        self._reset()
        return ReleaseAllResponse()
