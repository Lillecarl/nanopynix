"""gRPC EvalService handler for the worker subprocess.

Thread boundary: each open evaluator owns a dedicated Nix thread (its own
``NixThreadExecutor``, mirroring ``nanopynix.inproc``'s per-``EvalSession``
executor). All Nix C++ operations for a given evaluator run on that thread
via ``NixThreadExecutor.run()``/``run_sync()``. The gRPC handlers run on the
event loop and dispatch onto the evaluator's own thread — the event loop
never blocks on Nix.
"""

from __future__ import annotations

import logging
from typing import Any

import pydantic_core
from nanopynix_bindings import expr as nanopynix_expr
from nanopynix_proto.nix import common as common_pb
from nanopynix_proto.nix.common import (
    # Bare (unaliased) names for RPC-handler return-type annotations, as
    # opposed to the common_pb.X spelling used for runtime value access
    # elsewhere in this file: convert_handler_errors (_grpc_util.py) rebinds
    # each @worker_op entry point via functools.wraps, which copies these
    # PEP 563 deferred-string annotations onto a wrapper function whose own
    # __globals__ is _grpc_util's, not this module's. beartype resolves a
    # bare forward ref against the original defining module fine, but a
    # dotted one like "common_pb.ValueHandle" makes it try to import
    # "common_pb" as a real top-level module, which does not exist.
    FlakeRef,
    LockedFlake,
    ScalarValue,
    ValueHandle,
)
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
    EvalStatisticsRequest,
    EvalStatisticsResponse,
    EvalStringRequest,
    FindFlakeInputRequest,
    FindFlakeInputResponse,
    FlakeMetadataJsonRequest,
    FlakeMetadataJsonResponse,
    ForceJsonRequest,
    ForceJsonResponse,
    GetEvalVerbosityRequest,
    GetEvalVerbosityResponse,
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
    SetEvalCountersRequest,
    SetEvalCountersResponse,
    SetEvalVerbosityRequest,
    SetEvalVerbosityResponse,
    TypeNameRequest,
    TypeNameResponse,
    WriteLockFileRequest,
    WriteLockFileResponse,
)

from nanopynix._core._codec import python_to_scalar
from nanopynix._core._extract import locked_flake as _locked_flake
from nanopynix._core._objects import CoreEvalState, CoreValue
from nanopynix._wire import HandleKind
from nanopynix.exceptions import EvaluatorAbandonedError
from nanopynix.rpc.worker._grpc_util import worker_op, wrap_service_handlers
from nanopynix.rpc.worker._handle_registry import EvalEntry
from nanopynix.rpc.worker._proto_shape import proto_shape
from nanopynix.rpc.worker._state import WorkerState
from nanopynix.rpc.worker._worker_nix import NIX_EVALUATOR_STACK_SIZE, NixThreadExecutor

logger = logging.getLogger(__name__)

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


def find_evals_by_store(state: WorkerState, store_handle: int) -> list[int]:
    """Return the handles of every open evaluator bound to ``store_handle``."""
    return [handle for handle, entry in state.handles.iter_evals() if entry.store_handle == store_handle]


def release_eval_resources(state: WorkerState, eval_handle: int) -> None:
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


def close_eval_state(state: WorkerState, eval_handle: int) -> None:
    """Close the evaluator at ``eval_handle`` and all resources rooted by it.

    A no-op if ``eval_handle`` is already closed (e.g. a force-closed store
    already tore it down) — closing is idempotent, mirroring ``CloseEval``
    being safe to call after a server-side force-close.

    Callable from any worker thread (not just the event loop): the actual
    Nix C++ teardown is dispatched onto the evaluator's own dedicated
    executor via :meth:`NixThreadExecutor.run_sync`.
    """
    try:
        entry = state.handles.get_eval_entry(eval_handle)
    except KeyError:
        return
    state.handles.release(eval_handle)
    try:
        entry.executor.run_sync(release_eval_resources, state, eval_handle)
        entry.executor.run_sync(entry.eval_state.close)
    except EvaluatorAbandonedError:
        # The evaluator's thread is inside an operation that never answered its
        # interrupt, so neither the release nor the close can reach Nix.
        # Closing still succeeds, for the same reason inproc's
        # EvalSession.close does: a caller may always close, and refusing here
        # would leave the owning store unclosable too. The EvalState and the
        # values under it leak with the thread that owns them. `shutdown` below
        # queues the thread finalizer behind the runaway operation, so the
        # thread never outlives its Boehm GC registration.
        logger.warning(
            "closing an abandoned evaluator (handle %d) without its Nix teardown; "
            "its EvalState leaks with the thread that owns it",
            eval_handle,
        )
    finally:
        entry.executor.shutdown(wait=True)


@wrap_service_handlers
class EvalServiceHandler(EvalServiceBase):
    """gRPC handler for all eval/flake operations.

    Every method is written as a synchronous body and made an async gRPC entry
    point by :func:`~nanopynix.rpc.worker._grpc_util.worker_op`, which is also
    what ``_worker_store.py``'s handlers use. The two exceptions carry their own
    reason: ``open_eval`` allocates the handle the others dispatch on, so it has
    no evaluator thread to run on yet, and ``close_eval`` runs on the worker's
    shared executor rather than the evaluator's own, which is the thing it is
    shutting down.

    Each open evaluator owns a dedicated ``NixThreadExecutor``
    (``EvalEntry.executor``), so N concurrently open evaluators run on N
    separate Nix threads, mirroring ``nanopynix.inproc``'s per-``EvalSession``
    executor model.
    """

    def __init__(self, state: WorkerState) -> None:
        self._state: WorkerState = state

    # ── helpers (run on the Nix thread) ──────────────────────────

    def _get_entry(self, eval_handle: int) -> EvalEntry:
        try:
            return self._state.handles.get_eval_entry(eval_handle)
        except KeyError as exc:
            # Two different mistakes, and one message used to cover both.
            # Handle 0 is the wire's "none", so the caller never opened an
            # evaluator. Any other number is one this worker did not issue, or
            # issued and has since released -- and telling that caller to call
            # OpenEval named the wrong cause and hid the handle.
            if eval_handle == 0:
                raise RuntimeError("no EvalState is open — call OpenEval before evaluating") from exc
            raise RuntimeError(
                f"evaluator handle {eval_handle} is not open — this worker never issued it, or it is already closed",
            ) from exc

    def _get_es(self, eval_handle: int) -> CoreEvalState:
        return self._get_entry(eval_handle).eval_state

    async def _run(self, message: Any, operation: Any, *, executor: NixThreadExecutor | None = None) -> Any:
        # One lookup for both the thread and the level, because an evaluator
        # that holds a level of its own logs at that level on that thread. An
        # `executor` given here belongs to an evaluator this registry does not
        # know yet, which is `open_eval` only, so it follows the worker.
        entry = None if executor is not None else self._get_entry(message.eval_handle)
        return await self._state.run_request(
            request_id=message.request_id,
            executor=executor if entry is None else entry.executor,
            operation=operation,
            args=(message,),
            verbosity=None if entry is None else entry.verbosity,
        )

    async def open_eval(self, message: OpenEvalRequest) -> OpenEvalResponse:
        store = self._state.handles.get_store(message.store_handle)
        # 0 means "none", as everywhere else an optional handle crosses the
        # wire (see _worker_store's eval_store_handle). This used to be a
        # hardcoded None, which is why rpc's Session.eval could not accept the
        # build_store inproc has always taken.
        build_store = self._state.handles.get_store(message.build_store_handle) if message.build_store_handle else None
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
            args=(
                store,
                # The session's search path unless this evaluator named its
                # own, which matches what inproc does.
                list(message.nix_path) or self._state.nix_path,
                build_store,
                dict(message.eval_settings),
                dict(message.fetch_settings),
            ),
        )
        entry = EvalEntry(eval_state=eval_state, executor=executor, store_handle=message.store_handle)
        eval_handle = self._state.handles.allocate(entry, HandleKind.EVAL)
        return OpenEvalResponse(eval_handle=eval_handle)

    @worker_op
    def configure_eval(self, message: ConfigureEvalRequest) -> ConfigureEvalResponse:
        self._get_es(message.eval_handle).configure(dict(message.eval_settings), dict(message.fetch_settings))
        return ConfigureEvalResponse()

    # The one handler still written as a pair, because it is the one that
    # cannot run where the others do: `_run`'s default executor is the
    # evaluator's own thread, and this shuts that evaluator down. It runs on the
    # worker's shared executor instead, which `worker_op` has no way to say.
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
        return self._state.handles.get_value(handle)

    def _get_store(self, store_handle: int) -> Any:
        return self._state.handles.get_store(store_handle)

    def _call_arg_to_python(self, arg: common_pb.CallArg, es: Any) -> Any:  # noqa: PLR0911 -- tracked complexity/arg-count debt, see TODO.md
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

    @worker_op
    def eval_file(self, message: EvalFileRequest) -> ValueHandle:
        es = self._get_es(message.eval_handle)
        value = es.repl_eval_file(message.path) if es.repl_active() else es.eval_file(message.path)
        return self._export(value, message.eval_handle)

    @worker_op
    def repl_load_file(self, message: ReplLoadFileRequest) -> ValueHandle:
        return self._export(self._get_es(message.eval_handle).repl_load_file(message.path), message.eval_handle)

    @worker_op
    def eval_string(self, message: EvalStringRequest) -> ValueHandle:
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

    @worker_op
    def begin_repl(self, message: BeginReplRequest) -> BeginReplResponse:
        self._get_es(message.eval_handle).begin_repl()
        return BeginReplResponse()

    @worker_op
    def repl_process_line(self, message: ReplProcessLineRequest) -> ReplProcessLineResponse:
        value = self._get_es(message.eval_handle).repl_process_line(message.line, message.source_name)
        if value is None:
            return ReplProcessLineResponse(is_binding=True)
        return ReplProcessLineResponse(is_binding=False, value=self._export(value, message.eval_handle))

    @worker_op
    def repl_add_attrs(self, message: ReplAddAttrsRequest) -> ReplAddAttrsResponse:
        es = self._get_es(message.eval_handle)
        return ReplAddAttrsResponse(names=es.repl_add_attrs(self._resolve(message.handle)))

    @worker_op
    def repl_scope_names(self, message: ReplScopeNamesRequest) -> ReplScopeNamesResponse:
        return ReplScopeNamesResponse(names=self._get_es(message.eval_handle).repl_scope_names())

    @worker_op
    def reset_file_cache(self, message: ResetFileCacheRequest) -> ResetFileCacheResponse:
        self._get_es(message.eval_handle).reset_file_cache()
        return ResetFileCacheResponse()

    @worker_op
    def eval_statistics(self, message: EvalStatisticsRequest) -> EvalStatisticsResponse:
        return EvalStatisticsResponse(json=self._get_es(message.eval_handle).statistics_json())

    @worker_op
    def set_eval_counters(self, message: SetEvalCountersRequest) -> SetEvalCountersResponse:
        nanopynix_expr.set_eval_counters_enabled(message.enabled)
        return SetEvalCountersResponse(enabled=nanopynix_expr.eval_counters_enabled())

    # Named for the wire op, not the client method: the RPC is ForceJson and
    # really does transfer JSON. ValueProxy.to_python() decodes it.
    @worker_op
    def force_json(self, message: ForceJsonRequest) -> ForceJsonResponse:
        value = self._resolve(message.handle)
        # pydantic_core's Rust JSON encoder instead of stdlib json.dumps --
        # the tree is already known-valid JSON-compatible data straight out
        # of Nix's own to_json C++ binding, so this skips straight to
        # serialization rather than going through TypeAdapter validation.
        json_bytes = pydantic_core.to_json(value.to_json(copy_to_store=message.copy_to_store))
        return ForceJsonResponse(json=json_bytes.decode("utf-8"))

    @worker_op
    def as_scalar(self, message: AsScalarRequest) -> ScalarValue:
        accessor = _AS_SCALAR_ACCESSORS.get(message.nix_type)
        if accessor is None:
            raise ValueError(f"AsScalar does not support {message.nix_type!r}")
        value = getattr(self._resolve(message.handle), accessor)()
        return python_to_scalar(value)

    @worker_op
    def realise_string(self, message: RealiseStringRequest) -> RealiseStringResponse:
        return RealiseStringResponse(value=self._resolve(message.handle).realise_string())

    @worker_op
    def realise_argv(self, message: RealiseArgvRequest) -> RealiseArgvResponse:
        return RealiseArgvResponse(argv=self._resolve(message.handle).realise_argv())

    @worker_op
    def edit_location(self, message: EditLocationRequest) -> EditLocationResponse:
        location = self._resolve(message.handle).edit_location()
        return EditLocationResponse(path=location["path"], line=location["line"])

    @worker_op
    def attr(self, message: AttrRequest) -> ValueHandle:
        return self._export(self._resolve(message.handle).attr_get(message.name), message.eval_handle)

    @worker_op
    def list_get(self, message: ListGetRequest) -> ValueHandle:
        pv = self._resolve(message.handle)
        idx = message.index
        if idx < 0:
            idx += pv.list_length()
        if idx < 0:
            raise IndexError(f"list index out of range: {message.index}")
        return self._export(pv.list_get(idx), message.eval_handle)

    @worker_op
    def list_length(self, message: ListLengthRequest) -> ListLengthResponse:
        return ListLengthResponse(length=self._resolve(message.handle).list_length())

    @worker_op
    def attr_names(self, message: AttrNamesRequest) -> AttrNamesResponse:
        return AttrNamesResponse(names=self._resolve(message.handle).attr_names())

    @worker_op
    def has_attr(self, message: HasAttrRequest) -> HasAttrResponse:
        return HasAttrResponse(has=self._resolve(message.handle).has_attr(message.name))

    @worker_op
    def type_name(self, message: TypeNameRequest) -> TypeNameResponse:
        value = self._resolve(message.handle)
        value.force()
        type_name = value.type_name()
        nix_type = _NIX_TYPE_MAP.get(type_name, common_pb.NixType.UNSPECIFIED)
        return TypeNameResponse(type=nix_type)

    @worker_op
    def auto_call(self, message: AutoCallRequest) -> ValueHandle:
        return self._export(self._resolve(message.handle).auto_call(), message.eval_handle)

    @worker_op
    def call(self, message: CallRequest) -> ValueHandle:
        es = self._get_es(message.eval_handle)
        fn = self._resolve(message.handle)
        arguments: list[CoreValue] = []
        for arg in message.args:
            decoded = self._call_arg_to_python(arg, es)
            arguments.append(decoded if isinstance(decoded, CoreValue) else es.value_from_python(decoded))
        # The currying loop this replaces lived here; it is CoreValue.call's
        # now, so inproc gets the same semantics and the same intermediate
        # cleanup instead of a second implementation.
        return self._export(fn.call(*arguments), message.eval_handle)

    @worker_op
    def build(self, message: BuildRequest) -> BuildResponse:
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

    @worker_op
    def lock_flake(self, message: LockFlakeRequest) -> LockedFlake:
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

    @worker_op
    def call_locked_flake(self, message: CallLockedFlakeRequest) -> ValueHandle:
        lf = self._state.handles.get_locked_flake(message.handle)
        es = self._get_es(message.eval_handle)
        return self._export(es.call_locked_flake(lf), message.eval_handle)

    @worker_op
    def write_lock_file(self, message: WriteLockFileRequest) -> WriteLockFileResponse:
        lf = self._state.handles.get_locked_flake(message.handle)
        lf.write_lock_file()
        return WriteLockFileResponse()

    @worker_op
    def flake_metadata_json(self, message: FlakeMetadataJsonRequest) -> FlakeMetadataJsonResponse:
        lf = self._state.handles.get_locked_flake(message.handle)
        return FlakeMetadataJsonResponse(metadata_json=lf.metadata_json())

    @worker_op
    def find_flake_input(self, message: FindFlakeInputRequest) -> FindFlakeInputResponse:
        lf = self._state.handles.get_locked_flake(message.handle)
        return FindFlakeInputResponse(node=lf.find_input(list(message.path)))

    @worker_op
    def release_locked_flake(self, message: ReleaseLockedFlakeRequest) -> ReleaseLockedFlakeResponse:
        try:
            locked_flake = self._state.handles.get_locked_flake(message.handle)
        except KeyError:
            return ReleaseLockedFlakeResponse()
        locked_flake.close()
        self._state.handles.release(message.handle)
        return ReleaseLockedFlakeResponse()

    @worker_op
    def eval_flake(self, message: EvalFlakeRequest) -> ValueHandle:
        es = self._get_es(message.eval_handle)
        value = es.eval_flake(
            message.ref,
            write_lock_file=message.write_lock_file,
            flake_settings=dict(message.flake_settings),
        )
        return self._export(value, message.eval_handle)

    @worker_op
    def get_flake(self, message: GetFlakeRequest) -> FlakeRef:
        return self._get_es(message.eval_handle).get_flake(message.ref)

    @worker_op
    def get_eval_verbosity(self, message: GetEvalVerbosityRequest) -> GetEvalVerbosityResponse:
        """Return the level this evaluator logs at.

        Read from this evaluator's own Nix thread, and not from
        ``EvalEntry.verbosity``. The bindings hold the verbosity per thread,
        so what that thread reports is what Nix would actually filter this
        evaluator's messages at. ``WorkerService.GetVerbosity`` uses the same
        honest door for the worker.

        An evaluator that never set a level of its own reports the worker's,
        because ``_run`` passes ``None`` and ``run_request`` falls back.
        """
        return GetEvalVerbosityResponse(verbosity=common_pb.LogLevel(self._state.runtime.get_verbosity()))

    async def set_eval_verbosity(self, message: SetEvalVerbosityRequest) -> SetEvalVerbosityResponse:
        """Pin the level this evaluator logs at, and return it.

        Stored rather than dispatched, and deliberately not a ``worker_op``.
        ``run_request`` applies the level to the Nix thread at the start of
        every request, so the next request of this evaluator carries it, and a
        dispatch here would only set it on a thread that the next request
        overwrites.

        This writes no process-wide default, so it does not reach the threads
        Nix starts for itself. ``WorkerService.SetVerbosity`` is the door that
        does.
        """
        entry = self._get_entry(message.eval_handle)
        level = common_pb.LogLevel(message.verbosity)
        entry.verbosity = int(level)
        return SetEvalVerbosityResponse(verbosity=level)

    @worker_op
    def release(self, message: ReleaseRequest) -> ReleaseResponse:
        try:
            value = self._resolve(message.handle)
        except KeyError:
            return ReleaseResponse()
        value.close()
        self._state.handles.release(message.handle)
        return ReleaseResponse()
