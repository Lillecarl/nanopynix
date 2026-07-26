"""gRPC EvalService handler for the worker subprocess.

Thread boundary: each open evaluator owns a dedicated Nix thread (its own
``NixThreadExecutor``, mirroring ``nanopynix.inproc``'s per-``EvalSession``
executor). All Nix C++ operations for a given evaluator run on that thread
via ``NixThreadExecutor.run()``/``run_sync()``. The gRPC handlers run on the
event loop and dispatch onto the evaluator's own thread — the event loop
never blocks on Nix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pydantic_core
from nanopynix_bindings import expr as nanopynix_expr
from nanopynix_bindings import flake as nanopynix_flake
from nanopynix_proto.nix import common as common_pb
from nanopynix_proto.nix.eval import (
    AsScalarRequest,
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
    CloseEvalRequest,
    CloseEvalResponse,
    ConfigureEvalRequest,
    ConfigureEvalResponse,
    EditLocationRequest,
    EditLocationResponse,
    EvalFileRequest,
    EvalFlakeRequest,
    EvalServiceBase,
    EvalStringRequest,
    ForceJsonRequest,
    ForceJsonResponse,
    GetFlakeRequest,
    HasAttrRequest,
    HasAttrResponse,
    ListGetRequest,
    ListLengthRequest,
    ListLengthResponse,
    LockFlakeRequest,
    OpenEvalRequest,
    OpenEvalResponse,
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

from nanopynix._core._codec import python_to_scalar
from nanopynix._core._extract import flake_ref_attrs as _flake_ref_attrs
from nanopynix._core._extract import locked_flake as _locked_flake
from nanopynix._core._objects import CoreLockedFlake, CoreValue
from nanopynix._wire import HandleKind
from nanopynix.rpc.worker._grpc_util import wrap_service_handlers
from nanopynix.rpc.worker._proto_shape import proto_shape
from nanopynix.rpc.worker._worker_nix import NIX_EVALUATOR_STACK_SIZE, NixThreadExecutor

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

# The strict scalar reads AsScalar can serve, and the CoreValue accessor for
# each. Deliberately runs Nix's own force* rather than comparing type names
# here: a mismatch then raises the same nix::TypeError, with the same message,
# that an inproc caller gets, instead of a separately-invented client error.
_AS_SCALAR_ACCESSORS: dict[common_pb.NixType, str] = {
    common_pb.NixType.INT: "as_int",
    common_pb.NixType.FLOAT: "as_float",
    common_pb.NixType.BOOL: "as_bool",
    common_pb.NixType.STRING: "as_string",
}


@dataclass
class EvalEntry:
    """One worker-hosted evaluator: its state, dedicated Nix thread, and owning store."""

    eval_state: Any
    executor: NixThreadExecutor
    store_handle: int


def find_evals_by_store(state: Any, store_handle: int) -> list[int]:
    """Return the handles of every open evaluator bound to ``store_handle``."""
    return [handle for handle, entry in state.handles.iter_kind(HandleKind.EVAL) if entry.store_handle == store_handle]


def release_eval_resources(state: Any, eval_handle: int) -> None:
    """Release all worker handles owned by the evaluator at ``eval_handle``.

    Must run on that evaluator's own Nix thread — the ``.close()`` calls
    below touch thread-confined Nix C++ objects.
    """
    for handle, value in state.handles.iter_owned(eval_handle, HandleKind.VALUE):
        value.close()
        state.handles.release(handle)
    for handle, locked_flake in state.handles.iter_owned(eval_handle, HandleKind.LOCKED_FLAKE):
        locked_flake.close()
        state.handles.release(handle)


def close_eval_state(state: Any, eval_handle: int) -> None:
    """Close the evaluator at ``eval_handle`` and all resources rooted by it.

    A no-op if ``eval_handle`` is already closed (e.g. a force-closed store
    already tore it down) — closing is idempotent, mirroring ``CloseEval``
    being safe to call after a server-side force-close.

    Callable from any worker thread (not just the event loop): the actual
    Nix C++ teardown is dispatched onto the evaluator's own dedicated
    executor via :meth:`NixThreadExecutor.run_sync`.
    """
    try:
        entry: EvalEntry = state.handles.get_typed(eval_handle, HandleKind.EVAL)
    except KeyError:
        return
    state.handles.release(eval_handle)
    entry.executor.run_sync(release_eval_resources, state, eval_handle)
    try:
        entry.executor.run_sync(entry.eval_state.close)
    finally:
        entry.executor.shutdown(wait=True)


@wrap_service_handlers
class EvalServiceHandler(EvalServiceBase):
    """gRPC handler for all eval/flake operations.

    Every method (other than ``OpenEval``, which allocates the handle)
    follows the same dispatch pattern::

        async def method(self, message):
            return await self._get_executor(message.eval_handle).run(self._do_method, message)

    Each open evaluator owns a dedicated ``NixThreadExecutor``
    (``EvalEntry.executor``), so N concurrently open evaluators run on N
    separate Nix threads, mirroring ``nanopynix.inproc``'s per-``EvalSession``
    executor model.
    """

    def __init__(self, state: Any) -> None:
        self._state = state

    # ── helpers (run on the Nix thread) ──────────────────────────

    def _get_entry(self, eval_handle: int) -> EvalEntry:
        try:
            return self._state.handles.get_typed(eval_handle, HandleKind.EVAL)
        except KeyError as exc:
            raise RuntimeError("no EvalState is open — call OpenEval before evaluating") from exc

    def _get_es(self, eval_handle: int) -> Any:
        return self._get_entry(eval_handle).eval_state

    def _get_executor(self, eval_handle: int) -> NixThreadExecutor:
        return self._get_entry(eval_handle).executor

    async def _run(self, message: Any, operation: Any, *, executor: NixThreadExecutor | None = None) -> Any:
        selected_executor = executor or self._get_executor(message.eval_handle)
        return await self._state.run_request(
            request_id=message.request_id,
            executor=selected_executor,
            operation=operation,
            args=(message,),
        )

    async def open_eval(self, message: OpenEvalRequest) -> OpenEvalResponse:
        store = self._state.handles.get_typed(message.store_handle, HandleKind.STORE)
        executor = NixThreadExecutor(
            thread_name_prefix="nix-eval",
            thread_initializer=nanopynix_expr._enter_evaluator_thread,  # type: ignore[reportPrivateUsage] -- L1 GC thread-lifetime hook  # noqa: SLF001
            thread_finalizer=nanopynix_expr._exit_evaluator_thread,  # type: ignore[reportPrivateUsage] -- L1 GC thread-lifetime hook  # noqa: SLF001
            stack_size=NIX_EVALUATOR_STACK_SIZE,
        )
        eval_state = await self._state.run_request(
            request_id=message.request_id,
            executor=executor,
            operation=self._state.runtime.open_eval_state,
            args=(store, self._state.nix_path, None, dict(message.eval_settings), dict(message.fetch_settings)),
        )
        entry = EvalEntry(eval_state=eval_state, executor=executor, store_handle=message.store_handle)
        eval_handle = self._state.handles.allocate(entry, HandleKind.EVAL)
        return OpenEvalResponse(eval_handle=eval_handle)

    async def configure_eval(self, message: ConfigureEvalRequest) -> ConfigureEvalResponse:
        return await self._run(message, self._do_configure_eval)

    def _do_configure_eval(self, message: ConfigureEvalRequest) -> ConfigureEvalResponse:
        self._get_es(message.eval_handle).configure(dict(message.eval_settings), dict(message.fetch_settings))
        return ConfigureEvalResponse()

    async def close_eval(self, message: CloseEvalRequest) -> CloseEvalResponse:
        if self._state.executor is None:
            raise RuntimeError("worker executor is unavailable")
        return await self._run(message, self._do_close_eval, executor=self._state.executor)

    def _do_close_eval(self, message: CloseEvalRequest) -> CloseEvalResponse:
        close_eval_state(self._state, message.eval_handle)
        return CloseEvalResponse()

    def _export(self, value: Any, eval_handle: int) -> common_pb.ValueHandle:
        handle = self._state.handles.allocate(value, HandleKind.VALUE, owner=eval_handle)
        type_name = value.type_name()
        nix_type = _NIX_TYPE_MAP.get(type_name, common_pb.NixType.UNSPECIFIED)
        return common_pb.ValueHandle(handle=handle, type=nix_type)

    def _resolve(self, handle: int) -> Any:
        return self._state.handles.get_typed(handle, HandleKind.VALUE)

    def _get_store(self, store_handle: int) -> Any:
        return self._state.handles.get_typed(store_handle, HandleKind.STORE)

    def _call_arg_to_python(self, arg: common_pb.CallArg, es: Any) -> Any:  # noqa: PLR0911 tracked complexity/arg-count debt, see TODO.md
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
        return await self._run(message, self._do_eval_file)

    def _do_eval_file(self, message: EvalFileRequest) -> common_pb.ValueHandle:
        es = self._get_es(message.eval_handle)
        value = es.repl_eval_file(message.path) if es.repl_active() else es.eval_file(message.path)
        return self._export(value, message.eval_handle)

    async def repl_load_file(self, message: ReplLoadFileRequest) -> common_pb.ValueHandle:
        return await self._run(message, self._do_repl_load_file)

    def _do_repl_load_file(self, message: ReplLoadFileRequest) -> common_pb.ValueHandle:
        return self._export(self._get_es(message.eval_handle).repl_load_file(message.path), message.eval_handle)

    async def eval_string(self, message: EvalStringRequest) -> common_pb.ValueHandle:
        return await self._run(message, self._do_eval_string)

    def _do_eval_string(self, message: EvalStringRequest) -> common_pb.ValueHandle:
        es = self._get_es(message.eval_handle)
        value = (
            es.repl_eval_string(message.expr, message.source_name)
            if es.repl_active()
            else es.eval_string(
                message.expr,
                message.source_name,
            )
        )
        return self._export(value, message.eval_handle)

    async def begin_repl(self, message: BeginReplRequest) -> BeginReplResponse:
        return await self._run(message, self._do_begin_repl)

    def _do_begin_repl(self, message: BeginReplRequest) -> BeginReplResponse:
        self._get_es(message.eval_handle).begin_repl()
        return BeginReplResponse()

    async def repl_process_line(self, message: ReplProcessLineRequest) -> ReplProcessLineResponse:
        return await self._run(message, self._do_repl_process_line)

    def _do_repl_process_line(self, message: ReplProcessLineRequest) -> ReplProcessLineResponse:
        value = self._get_es(message.eval_handle).repl_process_line(message.line, message.source_name)
        if value is None:
            return ReplProcessLineResponse(is_binding=True)
        return ReplProcessLineResponse(is_binding=False, value=self._export(value, message.eval_handle))

    async def repl_add_attrs(self, message: ReplAddAttrsRequest) -> ReplAddAttrsResponse:
        return await self._run(message, self._do_repl_add_attrs)

    def _do_repl_add_attrs(self, message: ReplAddAttrsRequest) -> ReplAddAttrsResponse:
        es = self._get_es(message.eval_handle)
        return ReplAddAttrsResponse(names=es.repl_add_attrs(self._resolve(message.handle)))

    async def repl_scope_names(self, message: ReplScopeNamesRequest) -> ReplScopeNamesResponse:
        return await self._run(message, self._do_repl_scope_names)

    def _do_repl_scope_names(self, message: ReplScopeNamesRequest) -> ReplScopeNamesResponse:
        return ReplScopeNamesResponse(names=self._get_es(message.eval_handle).repl_scope_names())

    async def reset_file_cache(self, message: ResetFileCacheRequest) -> ResetFileCacheResponse:
        return await self._run(message, self._do_reset_file_cache)

    def _do_reset_file_cache(self, message: ResetFileCacheRequest) -> ResetFileCacheResponse:
        self._get_es(message.eval_handle).reset_file_cache()
        return ResetFileCacheResponse()

    # Named for the wire op, not the client method: the RPC is ForceJson and
    # really does transfer JSON. ValueProxy.to_python() decodes it.
    async def force_json(self, message: ForceJsonRequest) -> ForceJsonResponse:
        return await self._run(message, self._do_force_json)

    def _do_force_json(self, message: ForceJsonRequest) -> ForceJsonResponse:
        value = self._resolve(message.handle)
        # pydantic_core's Rust JSON encoder instead of stdlib json.dumps --
        # the tree is already known-valid JSON-compatible data straight out
        # of Nix's own to_json C++ binding, so this skips straight to
        # serialization rather than going through TypeAdapter validation.
        json_bytes = pydantic_core.to_json(value.to_json(copy_to_store=message.copy_to_store))
        return ForceJsonResponse(json=json_bytes.decode("utf-8"))

    async def as_scalar(self, message: AsScalarRequest) -> common_pb.ScalarValue:
        return await self._run(message, self._do_as_scalar)

    def _do_as_scalar(self, message: AsScalarRequest) -> common_pb.ScalarValue:
        accessor = _AS_SCALAR_ACCESSORS.get(message.nix_type)
        if accessor is None:
            raise ValueError(f"AsScalar does not support {message.nix_type!r}")
        value = getattr(self._resolve(message.handle), accessor)()
        return python_to_scalar(value)

    async def realise_string(self, message: RealiseStringRequest) -> RealiseStringResponse:
        return await self._run(message, self._do_realise_string)

    def _do_realise_string(self, message: RealiseStringRequest) -> RealiseStringResponse:
        return RealiseStringResponse(value=self._resolve(message.handle).realise_string())

    async def realise_argv(self, message: RealiseArgvRequest) -> RealiseArgvResponse:
        return await self._run(message, self._do_realise_argv)

    def _do_realise_argv(self, message: RealiseArgvRequest) -> RealiseArgvResponse:
        return RealiseArgvResponse(argv=self._resolve(message.handle).realise_argv())

    async def edit_location(self, message: EditLocationRequest) -> EditLocationResponse:
        return await self._run(message, self._do_edit_location)

    def _do_edit_location(self, message: EditLocationRequest) -> EditLocationResponse:
        location = self._resolve(message.handle).edit_location()
        return EditLocationResponse(path=location["path"], line=location["line"])

    async def attr(self, message: AttrRequest) -> common_pb.ValueHandle:
        return await self._run(message, self._do_attr)

    def _do_attr(self, message: AttrRequest) -> common_pb.ValueHandle:
        return self._export(self._resolve(message.handle).attr_get(message.name), message.eval_handle)

    async def list_get(self, message: ListGetRequest) -> common_pb.ValueHandle:
        return await self._run(message, self._do_list_get)

    def _do_list_get(self, message: ListGetRequest) -> common_pb.ValueHandle:
        pv = self._resolve(message.handle)
        idx = message.index
        if idx < 0:
            idx += pv.list_length()
        if idx < 0:
            raise IndexError(f"list index out of range: {message.index}")
        return self._export(pv.list_get(idx), message.eval_handle)

    async def list_length(self, message: ListLengthRequest) -> ListLengthResponse:
        return await self._run(message, self._do_list_length)

    def _do_list_length(self, message: ListLengthRequest) -> ListLengthResponse:
        return ListLengthResponse(length=self._resolve(message.handle).list_length())

    async def attr_names(self, message: AttrNamesRequest) -> AttrNamesResponse:
        return await self._run(message, self._do_attr_names)

    def _do_attr_names(self, message: AttrNamesRequest) -> AttrNamesResponse:
        return AttrNamesResponse(names=self._resolve(message.handle).attr_names())

    async def has_attr(self, message: HasAttrRequest) -> HasAttrResponse:
        return await self._run(message, self._do_has_attr)

    def _do_has_attr(self, message: HasAttrRequest) -> HasAttrResponse:
        return HasAttrResponse(has=self._resolve(message.handle).has_attr(message.name))

    async def type_name(self, message: TypeNameRequest) -> TypeNameResponse:
        return await self._run(message, self._do_type_name)

    def _do_type_name(self, message: TypeNameRequest) -> TypeNameResponse:
        value = self._resolve(message.handle)
        value.force()
        type_name = value.type_name()
        nix_type = _NIX_TYPE_MAP.get(type_name, common_pb.NixType.UNSPECIFIED)
        return TypeNameResponse(type=nix_type)

    async def auto_call(self, message: AutoCallRequest) -> common_pb.ValueHandle:
        return await self._run(message, self._do_auto_call)

    def _do_auto_call(self, message: AutoCallRequest) -> common_pb.ValueHandle:
        return self._export(self._resolve(message.handle).auto_call(), message.eval_handle)

    async def call(self, message: CallRequest) -> common_pb.ValueHandle:
        return await self._run(message, self._do_call)

    def _do_call(self, message: CallRequest) -> common_pb.ValueHandle:
        es = self._get_es(message.eval_handle)
        fn = self._resolve(message.handle)
        result = fn
        for arg in message.args:
            argument = self._call_arg_to_python(arg, es)
            value = argument if isinstance(argument, CoreValue) else es.value_from_python(argument)
            result = result.call(value)
        return self._export(result, message.eval_handle)

    async def build(self, message: BuildRequest) -> BuildResponse:
        return await self._run(message, self._do_build)

    def _do_build(self, message: BuildRequest) -> BuildResponse:
        entry = self._get_entry(message.eval_handle)
        value = self._resolve(message.handle)
        build_store = self._get_store(message.build_store_handle) if message.build_store_handle else None
        eval_store = None
        if build_store is not None and entry.store_handle != message.build_store_handle:
            eval_store = self._get_store(entry.store_handle)
        if self._state.collector is not None:
            self._state.log(
                "msg",
                int(common_pb.LogLevel.DEBUG),
                "eval build start "
                f"handle={message.handle} build_store_handle={message.build_store_handle or 'eval'} "
                f"eval_store={'separate' if eval_store is not None else 'none'} build_mode={message.build_mode}",
            )
        raw = value.build(
            build_store,
            message.build_mode,
            eval_store,
        )
        if self._state.collector is not None:
            shaped = proto_shape(raw)
            self._state.log(
                "msg",
                int(common_pb.LogLevel.DEBUG),
                f"eval build finish drv_path={shaped.get('drv_path', '')} outputs={sorted(shaped.get('outputs', {}))}",
            )
            return BuildResponse.from_dict(shaped)
        return BuildResponse.from_dict(proto_shape(raw))

    # ── flake methods ─────────────────────────────────────────────

    async def lock_flake(self, message: LockFlakeRequest) -> common_pb.LockedFlake:
        return await self._run(message, self._do_lock_flake)

    def _do_lock_flake(self, message: LockFlakeRequest) -> common_pb.LockedFlake:
        if self._state.collector is not None:
            self._state.log("msg", int(common_pb.LogLevel.INFO), f"lock_flake: parsing ref '{message.ref}'")
        es = self._get_es(message.eval_handle)

        if message.update_all is not None:
            update_inputs: bool | list[str] = message.update_all
        elif message.update_inputs_list is not None:
            update_inputs = list(message.update_inputs_list.inputs)
        else:
            update_inputs = False

        if self._state.collector is not None:
            self._state.log(
                "msg",
                int(common_pb.LogLevel.INFO),
                f"lock_flake: calling C++ lock_flake write_lock_file={message.write_lock_file}",
            )
        lf = es.lock_flake(
            message.ref,
            update_inputs=update_inputs,
            write_lock_file=message.write_lock_file,
            flake_settings=dict(message.flake_settings),
        )
        if self._state.collector is not None:
            self._state.log("msg", int(common_pb.LogLevel.INFO), "lock_flake: C++ lock_flake returned")
        handle = self._state.handles.allocate(lf, HandleKind.LOCKED_FLAKE, owner=message.eval_handle)

        lf_pb = _locked_flake(lf.require_raw())
        lf_pb.handle = handle
        return lf_pb

    async def call_locked_flake(self, message: CallLockedFlakeRequest) -> common_pb.ValueHandle:
        return await self._run(message, self._do_call_locked_flake)

    def _do_call_locked_flake(self, message: CallLockedFlakeRequest) -> common_pb.ValueHandle:
        lf: CoreLockedFlake = self._state.handles.get_typed(message.handle, HandleKind.LOCKED_FLAKE)
        es = self._get_es(message.eval_handle)
        return self._export(es.call_locked_flake(lf), message.eval_handle)

    async def write_lock_file(self, message: WriteLockFileRequest) -> WriteLockFileResponse:
        return await self._run(message, self._do_write_lock_file)

    def _do_write_lock_file(self, message: WriteLockFileRequest) -> WriteLockFileResponse:
        lf: CoreLockedFlake = self._state.handles.get_typed(message.handle, HandleKind.LOCKED_FLAKE)
        lf.write_lock_file()
        return WriteLockFileResponse()

    async def release_locked_flake(self, message: ReleaseLockedFlakeRequest) -> ReleaseLockedFlakeResponse:
        return await self._run(message, self._do_release_locked_flake)

    def _do_release_locked_flake(self, message: ReleaseLockedFlakeRequest) -> ReleaseLockedFlakeResponse:
        try:
            locked_flake: CoreLockedFlake = self._state.handles.get_typed(message.handle, HandleKind.LOCKED_FLAKE)
        except KeyError:
            return ReleaseLockedFlakeResponse()
        locked_flake.close()
        self._state.handles.release(message.handle)
        return ReleaseLockedFlakeResponse()

    async def eval_flake(self, message: EvalFlakeRequest) -> common_pb.ValueHandle:
        return await self._run(message, self._do_eval_flake)

    def _do_eval_flake(self, message: EvalFlakeRequest) -> common_pb.ValueHandle:
        es = self._get_es(message.eval_handle)
        value = es.eval_flake(
            message.ref,
            write_lock_file=message.write_lock_file,
            flake_settings=dict(message.flake_settings),
        )
        return self._export(value, message.eval_handle)

    async def get_flake(self, message: GetFlakeRequest) -> common_pb.FlakeRef:
        return await self._run(message, self._do_get_flake)

    def _do_get_flake(self, message: GetFlakeRequest) -> common_pb.FlakeRef:
        ref = nanopynix_flake.parse_flake_ref(message.ref)
        fr = nanopynix_flake.get_flake(self._get_es(message.eval_handle).require_raw(), ref)
        return common_pb.FlakeRef(attrs=_flake_ref_attrs(fr))

    async def release(self, message: ReleaseRequest) -> ReleaseResponse:
        return await self._run(message, self._do_release)

    def _do_release(self, message: ReleaseRequest) -> ReleaseResponse:
        try:
            value = self._resolve(message.handle)
        except KeyError:
            return ReleaseResponse()
        value.close()
        self._state.handles.release(message.handle)
        return ReleaseResponse()

    async def release_all(self, message: ReleaseAllRequest) -> ReleaseAllResponse:
        return await self._run(message, self._do_release_all)

    def _do_release_all(self, message: ReleaseAllRequest) -> ReleaseAllResponse:
        release_eval_resources(self._state, message.eval_handle)
        return ReleaseAllResponse()
