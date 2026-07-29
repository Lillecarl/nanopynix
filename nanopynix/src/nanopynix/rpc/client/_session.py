# ruff: noqa: ASYNC109
# Every `timeout` parameter in this module is threaded straight through to
# a grpclib stub call, which takes one. ASYNC109 wants a deadline instead,
# but the transport owns the timeout here, not this layer.
"""Eval session and ValueProxy for eval over gRPC."""

from __future__ import annotations

import queue
import threading
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Never, Protocol, cast, runtime_checkable

import anyio
import pydantic_core
from nanopynix_proto.nix.common import (
    CallArg,
    CallArgAttrs,
    CallArgList,
    LogLevel,
    RemoteCallArg,
)
from nanopynix_proto.nix.eval import (
    AsScalarRequest,
    AttrNamesRequest,
    AttrRequest,
    AutoCallRequest,
    BeginReplRequest,
    BuildRequest,
    CallLockedFlakeRequest,
    CallRequest,
    CloseEvalRequest,
    ConfigureEvalRequest,
    EditLocationRequest,
    EvalFileRequest,
    EvalFlakeRequest,
    EvalServiceBase,
    EvalStringRequest,
    ForceJsonRequest,
    GetFlakeRequest,
    HasAttrRequest,
    ListGetRequest,
    ListLengthRequest,
    LockFlakeRequest,
    OpenEvalRequest,
    RealiseArgvRequest,
    RealiseStringRequest,
    ReleaseLockedFlakeRequest,
    ReleaseRequest,
    ReplAddAttrsRequest,
    ReplLoadFileRequest,
    ReplProcessLineRequest,
    ReplScopeNamesRequest,
    ResetFileCacheRequest,
    TypeNameRequest,
    UpdateInputsList,
    WriteLockFileRequest,
)

from nanopynix._core._codec import python_to_scalar, scalar_to_python
from nanopynix._core._nix_core import build_mode_value
from nanopynix._typechecking import BEARTYPING, no_runtime_type_check
from nanopynix._wire import HandleKind
from nanopynix.exceptions import (
    EvalSessionClosedError,
    ForeignValueError,
    LockedFlakeReleasedError,
    StoreError,
    UnresolvedValueError,
    ValueReleasedError,
    build_error_from_result,
)
from nanopynix.models import FlakeRef, JsonScalar, JsonValue, LockedInput, NixType
from nanopynix.rpc.client._rpc_proxy import RpcProxyMixin
from nanopynix.settings import (
    DEFAULT_LINE_EDITORS,
    DEFAULT_RPC_TIMEOUT_SECONDS,
    NIX_PATH_SETTING_KEY,
    NixEvalSettings,
    NixFetchSettings,
    NixFlakeSettings,
    merge_defaults,
    reject_construction_time_keys,
)
from nanopynix.verbosity import LogLevelInput, normalize_log_level

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

    from betterproto2 import Message
    from nanopynix_bindings.store import BuildMode
    from nanopynix_proto.nix.common import LockedFlake as LockedFlakeProto

    from nanopynix.rpc.client._pool import WorkerClient
    from nanopynix.rpc.client.store import Store


# See EvalSettingsTarget in _core/_nix_core.py for why: without this beartype
# skips `EvalSession.__init__` entirely rather than checking its owner argument.
@runtime_checkable
class _EvalSessionOwner(Protocol):
    def claim_eval(self, eval_session: EvalSession) -> None: ...

    def release_eval(self, eval_session: EvalSession) -> None: ...


def _parse_nix_type(value: Any) -> NixType | None:
    """Parse a NixType from a proto ValueHandle type field or a string.

    Test mocks pass strings like ``"int"`` — map them to proto enum values.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return NixType.from_string(value)
    return value


# ════════════════════════════════════════════════════════════════════
# ValueProxy — thin gRPC client for exported eval handles
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class _ResolvedValue:
    handle: int
    nix_type: NixType | None
    lease: _Lease | None = None


@dataclass(frozen=True)
class _EvalOwnerToken:
    pass


@dataclass(frozen=True)
class _LazyValue:
    parent: ValueProxy
    selector: str | int
    nix_type: NixType | None = None


type _ValueState = _ResolvedValue | _LazyValue
type _ActiveFlag = list[bool]


@dataclass(frozen=True)
class _EvalOwner:
    token: _EvalOwnerToken
    active: _ActiveFlag | None = None

    def owns(self, value: ValueProxy) -> bool:
        return value._ctx.owner.token is self.token  # type: ignore[reportPrivateUsage] -- cross-class access  # noqa: SLF001


@dataclass(frozen=True, slots=True)
class _LeaseRef:
    """Opaque identity of one worker-side resource allocation."""

    kind: HandleKind
    handle: int
    generation: object

    @no_runtime_type_check  # beartype does not support `Never` return hints (BeartypeDecorHintPepUnsupportedException)
    def __copy__(self) -> Never:
        raise TypeError("worker cleanup leases cannot be copied")

    @no_runtime_type_check  # see __copy__
    def __deepcopy__(self, memo: dict[int, Any]) -> Never:
        del memo
        raise TypeError("worker cleanup leases cannot be copied")


class _Lease:
    """The single local claim on a worker allocation.

    The state is intentionally separate from :class:`_LeaseRef`: the latter
    can safely cross a finalizer boundary while this object serializes explicit
    release and finalizer fallback.
    """

    __slots__ = ("_lock", "_released", "ref")

    def __init__(self, ref: _LeaseRef) -> None:
        self.ref = ref
        self._lock = threading.Lock()
        self._released = False

    def claim(self) -> _LeaseRef | None:
        with self._lock:
            if self._released:
                return None
            self._released = True
            return self.ref


class _DeferredReleases:
    """Thread-safe, generation-scoped finalizer handoff for one eval session."""

    __slots__ = ("_active", "_generation", "_queue")

    def __init__(self) -> None:
        self._active = True
        self._generation = object()
        self._queue: queue.SimpleQueue[_LeaseRef] = queue.SimpleQueue()

    @property
    def generation(self) -> object:
        return self._generation

    def new_lease(self, kind: HandleKind, handle: int) -> _Lease:
        return _Lease(_LeaseRef(kind, handle, self._generation))

    def defer(self, ref: _LeaseRef) -> None:
        if self._active and ref.generation is self._generation:
            self._queue.put(ref)

    def drain(self) -> list[_LeaseRef]:
        refs: list[_LeaseRef] = []
        while True:
            try:
                ref = self._queue.get_nowait()
            except queue.Empty:
                break
            if ref.generation is self._generation:
                refs.append(ref)
        return refs

    def close(self) -> None:
        self._active = False
        self.drain()


def _finalize_lease(lease: _Lease, releases: _DeferredReleases) -> None:
    """Finalizer callback: queue cleanup; never make an RPC from GC."""
    ref = lease.claim()
    if ref is not None:
        releases.defer(ref)


# ════════════════════════════════════════════════════════════════════
# EvalProxy — auto-generated RPC proxy for the EvalService gRPC API
# ════════════════════════════════════════════════════════════════════


class EvalProxy(RpcProxyMixin, EvalServiceBase, rpc_service_base=EvalServiceBase):
    """Auto-generated proxy for all ``EvalService`` gRPC methods.

    Created by ``EvalSession.open()``. This proxy serializes its own operations
    because one EvalState is confined to one evaluator thread; unrelated Store
    RPCs remain free to run concurrently.
    """

    __slots__ = (
        "_active",
        "_draining_releases",
        "_eval_handle",
        "_operation_lock",
        "_releases",
        "_rpc_timeout",
        "_worker",
    )

    def __init__(
        self,
        worker: WorkerClient,
        releases: _DeferredReleases | None = None,
        rpc_timeout: float = DEFAULT_RPC_TIMEOUT_SECONDS,
    ) -> None:
        self._worker = worker
        self._releases = releases or _DeferredReleases()
        self._rpc_timeout = rpc_timeout
        self._active = True
        self._draining_releases = False
        self._operation_lock = anyio.Lock()
        self._eval_handle = 0

    def _check_active(self) -> None:
        if not self._active:
            raise EvalSessionClosedError("EvalProxy is invalid — the EvalSession has been closed")

    def deactivate(self) -> None:
        self._active = False

    async def _rpc_proxy_call(self, method_name: str, message: Message) -> Any:
        async with self._operation_lock:
            self._check_active()
            if not self._draining_releases:
                await self._drain_deferred_releases_locked()
            if method_name != "open_eval":
                message.eval_handle = self._eval_handle  # type: ignore[attr-defined] -- every non-OpenEval EvalService request carries eval_handle=100
            method = getattr(self._worker.eval_stub, method_name)
            return await self._worker.invoke(method, message, timeout=self._rpc_timeout)

    async def drain_deferred_releases(self) -> None:
        """Best-effort cleanup at a safe worker-RPC boundary."""
        async with self._operation_lock:
            await self._drain_deferred_releases_locked()

    async def _drain_deferred_releases_locked(self) -> None:
        """Drain finalizer work while :attr:`_operation_lock` is held."""
        if self._draining_releases:
            return
        self._draining_releases = True
        try:
            for ref in self._releases.drain():
                if ref.kind == HandleKind.VALUE:
                    method = self._worker.eval_stub.release
                    await self._worker.invoke(
                        method,
                        ReleaseRequest(handle=ref.handle, eval_handle=self._eval_handle),
                        timeout=self._rpc_timeout,
                    )
                elif ref.kind == HandleKind.LOCKED_FLAKE:
                    method = self._worker.eval_stub.release_locked_flake
                    await self._worker.invoke(
                        method,
                        ReleaseLockedFlakeRequest(handle=ref.handle, eval_handle=self._eval_handle),
                        timeout=self._rpc_timeout,
                    )
                else:
                    raise RuntimeError(f"unknown deferred lease kind: {ref.kind}")
        finally:
            self._draining_releases = False

    async def _store_proxy_call(self, method_name: str, message: Message) -> Any:
        self._check_active()
        method = getattr(self._worker.store_stub, method_name)
        return await self._worker.invoke(method, message, timeout=self._rpc_timeout)


@dataclass(frozen=True)
class _EvalProxyContext:
    proxy: EvalProxy
    owner: _EvalOwner
    timeout: float | None = None
    store_handle: int = 1
    session_id: str = ""
    releases: _DeferredReleases = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "releases", self.proxy._releases)  # type: ignore[reportPrivateUsage] -- context shares its proxy's session cleanup queue  # noqa: SLF001

    def with_timeout(self, timeout: float | None) -> _EvalProxyContext:
        if timeout is None:
            return self
        return _EvalProxyContext(self.proxy, self.owner, timeout, self.store_handle, self.session_id)

    def resolve_timeout(self, override: float | None) -> float | None:
        return override if override is not None else self.timeout

    def value(self, handle: int, typ: Any) -> ValueProxy:
        return ValueProxy(
            self,
            _ResolvedValue(
                handle=handle,
                nix_type=_parse_nix_type(typ),
                lease=self.releases.new_lease(HandleKind.VALUE, handle),
            ),
        )

    def child(
        self,
        parent: ValueProxy,
        selector: str | int,
        *,
        timeout: float | None = None,
    ) -> ValueProxy:
        return ValueProxy(
            self.with_timeout(self.resolve_timeout(timeout)),
            _LazyValue(parent=parent, selector=selector),
        )


type NixArg = ValueProxy | JsonScalar | list[NixArg] | dict[str, NixArg]


class ValueProxy:
    """Proxy for a Nix Value exported on the remote worker.

    Lifetime is tied to the ``EvalSession`` that created it — all gRPC
    methods raise ``EvalSessionClosedError`` after the session exits.

    Supports ``async with`` for early release.
    """

    __slots__ = (
        "__weakref__",
        "_ctx",
        "_finalizer",
        "_released",
        "_state",
    )

    def __init__(self, ctx: _EvalProxyContext, state: _ValueState) -> None:
        self._ctx = ctx
        if isinstance(state, _ResolvedValue) and state.lease is None:
            state = _ResolvedValue(state.handle, state.nix_type, ctx.releases.new_lease(HandleKind.VALUE, state.handle))
        self._state = state
        self._released = False
        self._finalizer: Any = None
        if isinstance(state, _ResolvedValue):
            self._finalizer = weakref.finalize(self, _finalize_lease, self._lease, ctx.releases)

    async def __aenter__(self) -> ValueProxy:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

    @no_runtime_type_check  # beartype does not support `Never` return hints (BeartypeDecorHintPepUnsupportedException)
    def __copy__(self) -> Never:
        raise TypeError("ValueProxy cannot be copied; use normal assignment to share it")

    @no_runtime_type_check  # see __copy__
    def __deepcopy__(self, memo: dict[int, Any]) -> Never:
        del memo
        raise TypeError("ValueProxy cannot be copied; use normal assignment to share it")

    def _check_active(self) -> None:
        active = self._ctx.owner.active
        if active is not None and not active[0]:
            raise EvalSessionClosedError("ValueProxy is invalid — the EvalSession has been closed")
        if self._released:
            raise ValueReleasedError("ValueProxy has been released")

    @property
    def handle(self) -> int:
        if not isinstance(self._state, _ResolvedValue):
            raise UnresolvedValueError("ValueProxy has not been resolved yet")
        return self._state.handle

    @property
    def _resolved(self) -> _ResolvedValue:
        if not isinstance(self._state, _ResolvedValue):
            raise UnresolvedValueError("ValueProxy has not been resolved yet")
        return self._state

    @property
    def _lease(self) -> _Lease:
        lease = self._resolved.lease
        if lease is None:
            raise RuntimeError("resolved ValueProxy has no cleanup lease")
        return lease

    @property
    def nix_type(self) -> NixType:
        return self._state.nix_type or NixType.UNSPECIFIED

    async def _ensure_resolved(self, *, timeout: float | None = None) -> None:
        self._check_active()
        if isinstance(self._state, _ResolvedValue):
            return
        lazy = self._state
        await lazy.parent._ensure_resolved(timeout=timeout)  # noqa: SLF001 -- lazy.parent is another ValueProxy of this same class, resolved recursively
        parent = lazy.parent._resolved  # noqa: SLF001 -- same-class access, reading the now-resolved parent ValueProxy's state
        if isinstance(lazy.selector, str):
            handle = await self._ctx.proxy.attr(AttrRequest(handle=parent.handle, name=lazy.selector))
        else:
            handle = await self._ctx.proxy.list_get(ListGetRequest(handle=parent.handle, index=lazy.selector))
        self._state = _ResolvedValue(
            handle=handle.handle,
            nix_type=_parse_nix_type(handle.type),
            lease=self._ctx.releases.new_lease(HandleKind.VALUE, handle.handle),
        )
        self._finalizer = weakref.finalize(self, _finalize_lease, self._lease, self._ctx.releases)

    async def _ensure_type(self, *, timeout: float | None = None) -> NixType:
        await self._ensure_resolved(timeout=timeout)
        cached = self._state.nix_type
        if cached not in (None, NixType.THUNK, NixType.UNSPECIFIED):
            return cached
        resp = await self._ctx.proxy.type_name(TypeNameRequest(handle=self.handle))
        resolved_type = _parse_nix_type(resp.type) or NixType.UNSPECIFIED
        self._state = _ResolvedValue(handle=self.handle, nix_type=resolved_type, lease=self._resolved.lease)
        return resolved_type

    async def _encode_call_arg(self, value: NixArg, *, timeout: float | None) -> CallArg:
        if isinstance(value, ValueProxy):
            if not self._ctx.owner.owns(value):
                raise ForeignValueError("cannot pass a ValueProxy from another EvalSession")
            await value._ensure_resolved(timeout=timeout)  # noqa: SLF001 -- value is another ValueProxy of this same class, ownership already checked above
            return CallArg(remote_value=RemoteCallArg(handle=value.handle))
        if isinstance(value, list):
            return CallArg(
                list=CallArgList(items=[await self._encode_call_arg(item, timeout=timeout) for item in value]),
            )
        if isinstance(value, dict):
            return CallArg(
                attrs=CallArgAttrs(
                    entries={key: await self._encode_call_arg(item, timeout=timeout) for key, item in value.items()},
                ),
            )
        return CallArg(scalar=python_to_scalar(value, on_unsupported="stringify"))

    async def _as_scalar(self, typ: NixType, *, timeout: float | None) -> Any:
        """Strict scalar read, type-checked by Nix in the worker.

        The check runs in the worker, which is what makes both engines raise
        the same ``NixTypeError`` with the same Nix-authored message for the
        same mistake. ``force_as`` used to sit alongside this doing the
        opposite -- comparing type names client-side and raising
        ``WrongNixTypeError`` -- and was deleted for it: it was the rpc
        spelling of ``as_*`` reached by NixType argument instead of by name,
        with worse behaviour (it rejected an int for FLOAT where Nix's
        ``forceFloat`` widens) and no consumer outside docs examples.
        """
        await self._ensure_resolved(timeout=timeout)
        scalar = await self._ctx.proxy.as_scalar(AsScalarRequest(handle=self.handle, nix_type=typ))
        return scalar_to_python(scalar)

    async def as_int(self, *, timeout: float | None = None) -> int:
        """Force this value and return it as ``int``. Raises if not an int."""
        return cast("int", await self._as_scalar(NixType.INT, timeout=timeout))

    async def as_float(self, *, timeout: float | None = None) -> float:
        """Force this value and return it as ``float``. Raises if not a float.

        An int widens to float, because Nix's own ``forceFloat`` accepts one --
        the same rule that lets ``1 + 1.0`` evaluate.
        """
        return float(cast("float", await self._as_scalar(NixType.FLOAT, timeout=timeout)))

    async def as_bool(self, *, timeout: float | None = None) -> bool:
        """Force this value and return it as ``bool``. Raises if not a bool."""
        return cast("bool", await self._as_scalar(NixType.BOOL, timeout=timeout))

    async def as_string(self, *, timeout: float | None = None) -> str:
        """Force this value and return it as ``str``. Raises if not a string.

        Nix string context is dropped, not rejected; use ``realise_string()``
        when the store paths it names have to exist.
        """
        return cast("str", await self._as_scalar(NixType.STRING, timeout=timeout))

    async def apply(self, function: str | ValueProxy, *, timeout: float | None = None) -> ValueProxy:
        """Apply a Nix function to this value and return the result.

        The general form of "do a Nix thing to this value". A ``str`` is any
        Nix function expression -- a builtin, or a lambda::

            await (await value.apply("builtins.toString")).as_string()
            await (await value.apply("builtins.length")).as_int()
            await (await value.apply("x: x * 2")).as_int()

        Passing an already-evaluated function instead of a string hoists the
        evaluation out of a loop, which is the whole of what a memoised
        ``apply`` would buy::

            to_string = await eval_session.string("builtins.toString")
            for value in values:
                await (await value.apply(to_string)).as_string()

        This deliberately replaces a family of dedicated coercion methods.
        ``coerce_str`` was only ``builtins.toString`` spelled a second way, and
        the int/float/bool variants had no Nix counterpart at all.
        """
        if isinstance(function, str):
            handle = await self._ctx.proxy.eval_string(EvalStringRequest(expr=function, source_name="<apply>"))
            function = self._ctx.value(handle.handle, handle.type)
        return await function.call(self, timeout=timeout)

    async def to_python(self, *, copy_to_store: bool = False, timeout: float | None = None) -> JsonValue:
        """Convert the whole value tree to plain Python data, in one C++ pass.

        This is Nix's own ``printValueAsJSON`` with ``strict=true``, so the
        rules are Nix's rather than ours: an attrset with ``__toString``
        becomes that string, an attrset with ``outPath`` (a derivation, for
        instance) becomes the output path, and anything Nix will not flatten
        -- a function -- is an error rather than a placeholder.

        Converting *is* lossy, deliberately: that ``outPath`` rule is what
        makes a derivation convertible at all, since ``drv.out`` is ``drv``.
        Contrast the ``as_*`` accessors, which convert nothing and raise when
        the value is not already of the type asked for.

        This replaces ``to_python``, which walked the tree itself, died on
        any derivation, and disagreed with the in-process engine about what
        happens to function leaves.

        Args:
            copy_to_store: If True, a ``path`` value is copied into the Nix
                store and rendered as a store path -- what string
                interpolation of a path does. If False (default), it renders
                as a literal filesystem path, matching ``nix eval --json``.
            timeout: Maximum seconds to wait for the value to resolve. None
                (default) waits indefinitely.
        """
        await self._ensure_resolved(timeout=timeout)
        # The wire op stays ForceJson: it really does transfer JSON, and the
        # conversion to Python objects happens here. Only the Python-facing
        # name changed.
        resp = await self._ctx.proxy.force_json(
            ForceJsonRequest(handle=self.handle, copy_to_store=copy_to_store),
        )
        # pydantic_core's Rust JSON decoder instead of stdlib json.loads.
        return pydantic_core.from_json(resp.json)

    async def realise_string(self, *, timeout: float | None = None) -> str:
        """Coerce this value to a string and realise its Nix string context."""
        await self._ensure_resolved(timeout=timeout)
        response = await self._ctx.proxy.realise_string(RealiseStringRequest(handle=self.handle))
        return response.value

    async def realise_argv(self, *, timeout: float | None = None) -> list[str]:
        """Coerce a Nix list to argv and realise all of its string contexts."""
        await self._ensure_resolved(timeout=timeout)
        response = await self._ctx.proxy.realise_argv(RealiseArgvRequest(handle=self.handle))
        return list(response.argv)

    async def edit_location(self, *, timeout: float | None = None) -> tuple[str, int]:
        """Return the physical file path and line Nix would open for this value."""
        await self._ensure_resolved(timeout=timeout)
        response = await self._ctx.proxy.edit_location(EditLocationRequest(handle=self.handle))
        return response.path, response.line

    async def build(
        self,
        *,
        build_mode: BuildMode | int | None = None,
        store: Store | None = None,
        timeout: float | None = None,
    ) -> dict[str, str]:
        """Build the derivation represented by this evaluated value."""
        build_store_handle = self._build_store_handle(store)
        return await self._build_evaluated_value(build_mode_value(build_mode), build_store_handle, timeout=timeout)

    def _build_store_handle(self, store: Store | None) -> int:
        if store is None:
            return self._ctx.store_handle
        if store._session_id != self._ctx.session_id:  # type: ignore[reportPrivateUsage] -- cross-class access  # noqa: SLF001
            raise ValueError("Store belongs to a different session")
        return store.store_handle

    async def _build_evaluated_value(
        self,
        build_mode: int,
        build_store_handle: int,
        *,
        timeout: float | None,
    ) -> dict[str, str]:
        await self._ensure_resolved(timeout=timeout)
        request_build_store_handle = 0 if build_store_handle == self._ctx.store_handle else build_store_handle
        build_response = await self._ctx.proxy.build(
            BuildRequest(
                handle=self.handle,
                build_mode=build_mode,
                build_store_handle=request_build_store_handle,
            ),
        )
        if not build_response.results:
            raise StoreError(
                "StoreError",
                f"build returned no result for evaluated derivation {build_response.drv_path}",
            )
        result = build_response.results[0]
        if not result.success:
            # `result.status` is Nix's own failure kind (e.g. "hash-mismatch"),
            # so the exception type is derived rather than hardcoded -- and it
            # matches what inproc raises for the same failure.
            raise build_error_from_result(
                status=result.status,
                error_msg=result.error_msg or f"failed to build evaluated derivation {build_response.drv_path}",
                drv_path=build_response.drv_path,
            )
        outputs = dict(build_response.outputs)
        if not outputs:
            raise StoreError("StoreError", f"derivation {build_response.drv_path} has no outputs")
        return outputs

    # ── navigation ─────────────────────────────────────────────────

    def attr(self, name: str, *, timeout: float | None = None) -> ValueProxy:
        """Return a lazy child proxy for attrset attribute ``name``.

        No RPC is made until the result is forced or resolved.
        """
        self._check_active()
        return self._ctx.child(self, name, timeout=timeout)

    def list_get(self, index: int, *, timeout: float | None = None) -> ValueProxy:
        """Return a lazy child proxy for list index ``index``.

        No RPC is made until the result is forced or resolved.
        """
        self._check_active()
        return self._ctx.child(self, index, timeout=timeout)

    async def list_length(self, *, timeout: float | None = None) -> int:
        """Force this value as a list and return its length."""
        await self._ensure_resolved(timeout=timeout)
        resp = await self._ctx.proxy.list_length(ListLengthRequest(handle=self.handle))
        return resp.length

    async def attr_names(self, *, timeout: float | None = None) -> list[str]:
        """Force this value as an attrset and return its attribute names."""
        await self._ensure_resolved(timeout=timeout)
        resp = await self._ctx.proxy.attr_names(AttrNamesRequest(handle=self.handle))
        return resp.names

    async def as_dict(self, *, timeout: float | None = None) -> dict[str, ValueProxy]:
        """Force this value as an attrset; keys now, values still lazy.

        See :meth:`nanopynix.inproc.Value.as_dict` -- same contract, same
        reasoning. Costs exactly one round trip, the ``attr_names`` that
        produces the keys: the proxies are lazy and make no call of their own
        until forced.
        """
        names = await self.attr_names(timeout=timeout)
        return {name: self.attr(name, timeout=timeout) for name in names}

    async def as_list(self, *, timeout: float | None = None) -> list[ValueProxy]:
        """Force this value as a list; length now, elements still lazy.

        The list half of :meth:`as_dict`; one round trip, for ``list_length``.
        """
        length = await self.list_length(timeout=timeout)
        return [self.list_get(index, timeout=timeout) for index in range(length)]

    async def has_attr(self, name: str, *, timeout: float | None = None) -> bool:
        """Force this value as an attrset and return whether ``name`` is present."""
        await self._ensure_resolved(timeout=timeout)
        resp = await self._ctx.proxy.has_attr(HasAttrRequest(handle=self.handle, name=name))
        return resp.has

    async def call(self, *args: NixArg, timeout: float | None = None) -> ValueProxy:
        """Call this value as a Nix function with ``args``.

        Args:
            *args: Each argument may be a ``ValueProxy`` from the same
                ``EvalSession``, or a JSON-scalar/list/dict, which is encoded
                as a Nix value on the worker side.
            timeout: Maximum seconds to wait for this value to resolve to a
                function before calling it. None (default) waits indefinitely.

        Raises:
            TypeError: No arguments were given; Nix has no nullary
                application.
            NixTypeError: This value is not a function. Nix decides that in
                the worker rather than the proxy pre-checking it here, which
                is what makes the two engines agree: the pre-check raised
                ``WrongNixTypeError``, an ``ObjectMisuseError`` unrelated to
                ``NixTypeError``, so no single ``except`` covered both engines
                for the same mistake.
            ForeignValueError: An argument ``ValueProxy`` belongs to a
                different ``EvalSession``.
        """
        # Refused here rather than in the worker so that both engines raise the
        # same Python TypeError. CoreValue.call guards this too and is the real
        # rule; letting it fire worker-side instead would send the failure back
        # through the wire's exception mapping and arrive as NixTypeError.
        if not args:
            raise TypeError("call() needs at least one argument; Nix has no nullary application")
        await self._ensure_resolved(timeout=timeout)
        call_args = [await self._encode_call_arg(arg, timeout=timeout) for arg in args]
        result = await self._ctx.proxy.call(CallRequest(handle=self.handle, args=call_args))
        return self._ctx.value(result.handle, result.type)

    async def auto_call(self, *, timeout: float | None = None) -> ValueProxy:
        """Apply Nix top-level auto-call semantics to a function value."""
        await self._ensure_resolved(timeout=timeout)
        result = await self._ctx.proxy.auto_call(AutoCallRequest(handle=self.handle))
        return self._ctx.value(result.handle, result.type)

    async def __call__(self, *args: NixArg, timeout: float | None = None) -> ValueProxy:
        return await self.call(*args, timeout=timeout)

    async def get_type(self, *, timeout: float | None = None) -> NixType:
        """Evaluate to WHNF and return the resulting Nix type.

        This is also *the* way to force a value for its side effects (to make
        it raise, say). ``force()`` used to sit next to this and was deleted:
        answering "what type is this" requires forcing first -- the worker's
        ``type_name`` handler calls ``value.force()`` before ``type_name()`` --
        so ``force() -> NixType`` would have been this method under a second
        name. Nix models it the same way: ``forceValue`` returns void and you
        read ``value->type()`` afterwards.
        """
        return await self._ensure_type(timeout=timeout)

    # ── release ────────────────────────────────────────────────────

    async def release(self, *, timeout: float | None = None) -> None:
        """Release the worker-side handle for this value.

        Idempotent — calling this more than once, or on an unresolved
        (never-forced) value, is a no-op. Also invoked automatically by
        ``async with``/garbage collection.
        """
        if self._released:
            return
        if not isinstance(self._state, _ResolvedValue):
            self._released = True
            return
        self._check_active()
        ref = self._lease.claim()
        self._released = True
        if self._finalizer is not None:
            self._finalizer.detach()
        if ref is None:
            return
        try:
            await self._ctx.proxy.release(ReleaseRequest(handle=ref.handle))
        except BaseException:
            self._ctx.releases.defer(ref)
            raise


# ════════════════════════════════════════════════════════════════════
# _ChildProxy — lazy proxy for attrset attributes / list elements
# ════════════════════════════════════════════════════════════════════

_ChildProxy = ValueProxy


# ════════════════════════════════════════════════════════════════════
# EvalSession
# ════════════════════════════════════════════════════════════════════


@dataclass
class LockedFlakeHandle:
    """Session-bound handle for an in-memory locked flake."""

    _session: EvalSession = field(repr=False)
    handle: int
    description: str
    inputs: dict[str, LockedInput]
    _released: bool = field(default=False, init=False, repr=False)
    _lease: _Lease = field(init=False, repr=False)
    _finalizer: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        releases = self._session._deferred_releases()  # type: ignore[reportPrivateUsage] -- session owns resource cleanup generation  # noqa: SLF001
        self._lease = releases.new_lease(HandleKind.LOCKED_FLAKE, self.handle)
        self._finalizer = weakref.finalize(self, _finalize_lease, self._lease, releases)

    @no_runtime_type_check  # beartype does not support `Never` return hints (BeartypeDecorHintPepUnsupportedException)
    def __copy__(self) -> Never:
        raise TypeError("LockedFlakeHandle cannot be copied; use normal assignment to share it")

    @no_runtime_type_check  # see __copy__
    def __deepcopy__(self, memo: dict[int, Any]) -> Never:
        del memo
        raise TypeError("LockedFlakeHandle cannot be copied; use normal assignment to share it")

    def _check_active(self) -> None:
        if self._released:
            raise LockedFlakeReleasedError("LockedFlakeHandle has been released")

    async def eval(self, *, timeout: float | None = None) -> ValueProxy:
        """Evaluate this locked flake's outputs, calling its ``outputs`` function.

        Uses the in-memory lock from a prior ``lock_flake()``, so an updated
        lock can be evaluated before it is written to disk.
        """
        self._check_active()
        return await self._session._eval_locked_flake(self, timeout=timeout)  # type: ignore[reportPrivateUsage] -- LockedFlakeHandle is its session's public door onto this private by-handle call  # noqa: SLF001 -- same reason

    async def write_lock_file(self, *, timeout: float | None = None) -> None:
        """Persist this locked flake's lock file to disk."""
        self._check_active()
        await self._session._write_lock_file(self, timeout=timeout)  # type: ignore[reportPrivateUsage] -- LockedFlakeHandle is its session's public door onto this private by-handle call  # noqa: SLF001 -- same reason

    async def release(self, *, timeout: float | None = None) -> None:
        """Release the worker-side handle for this locked flake. Idempotent."""
        if self._released:
            return
        self._released = True
        self._finalizer.detach()
        ref = self._lease.claim()
        if ref is None:
            return
        try:
            await self._session.release_locked_flake(self, timeout=timeout)
        except BaseException:
            self._session._deferred_releases().defer(ref)  # type: ignore[reportPrivateUsage] -- session owns cleanup retry queue  # noqa: SLF001
            raise


class EvalSession:
    """Own the parent Session's one live EvalState.

    All ``ValueProxy`` instances created through this session become
    invalid after ``__aexit__`` — their RPC methods raise ``EvalSessionClosedError``.
    """

    __slots__ = (
        "_active",
        "_build_store",
        "_ctx",
        "_eval_settings",
        "_fetch_settings",
        "_flake_defaults",
        "_line_editors",
        "_owner",
        "_owner_session",
        "_proxy",
        "_releases",
        "_repl_begun",
        "_rpc_timeout",
        "_session_id",
        "_store",
        "_store_handle",
        "_timeout",
        "_worker",
    )

    def __init__(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
        self,
        worker: WorkerClient,
        owner_session: _EvalSessionOwner | None = None,
        store_handle: int = 1,
        timeout: float | None = None,
        session_id: str = "",
        rpc_timeout: float = DEFAULT_RPC_TIMEOUT_SECONDS,
        line_editors: Sequence[str] = DEFAULT_LINE_EDITORS,
        store: Store | None = None,
        build_store: Store | None = None,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
        flake_defaults: NixFlakeSettings | None = None,
    ) -> None:
        self._worker = worker
        self._owner_session = owner_session
        self._store = store
        self._store_handle = store_handle
        self._build_store = build_store
        self._session_id = session_id
        self._proxy: EvalProxy | None = None
        self._timeout = timeout
        self._rpc_timeout = rpc_timeout
        self._line_editors = tuple(line_editors)
        self._repl_begun = False
        self._eval_settings = eval_settings
        self._fetch_settings = fetch_settings
        # Not `_flake_settings`: a flake setting belongs to one flake
        # operation, not to the evaluator, so this is only what those
        # operations fall back to. `Session.eval` passes the session's.
        self._flake_defaults = flake_defaults if flake_defaults is not None else NixFlakeSettings()
        self._active: list[bool] = [False]
        self._owner = _EvalOwner(_EvalOwnerToken(), self._active)
        self._ctx: _EvalProxyContext | None = None
        self._releases: _DeferredReleases | None = None

    async def __aenter__(self) -> EvalSession:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def open(self) -> None:
        """Open this session's EvalState. Called automatically by ``async with``."""
        if self._proxy is not None:
            return
        if self._owner_session is not None:
            self._owner_session.claim_eval(self)
        releases = _DeferredReleases()
        proxy = EvalProxy(self._worker, releases, self._rpc_timeout)
        rendered_eval = self._eval_settings.to_worker_settings() if self._eval_settings is not None else {}
        # nix_path travels in its own field, not in the settings map: Nix reads
        # the search path while it builds the EvalState, and the worker applies
        # the map to the settings registry, which is too late. Empty means "use
        # the session's".
        rendered_eval.pop(NIX_PATH_SETTING_KEY, None)
        nix_path = list(self._eval_settings.nix_path or ()) if self._eval_settings is not None else []
        rendered_fetch = self._fetch_settings.to_worker_settings() if self._fetch_settings is not None else {}
        # Only named when this evaluator actually has one. An empty search path
        # means "use the session's", which is what leaving the field alone
        # already says, and not naming it keeps this request buildable against
        # a generated proto that predates the field. A caller that does use the
        # feature still gets a hard error there, which is correct: the worker
        # on the other side cannot honour it.
        request_nix_path: dict[str, Any] = {"nix_path": nix_path} if nix_path else {}
        try:
            response = await proxy.open_eval(
                OpenEvalRequest(
                    store_handle=self._store_handle,
                    # 0 is the wire's "none" for an optional handle
                    build_store_handle=0 if self._build_store is None else self._build_store.store_handle,
                    eval_settings=rendered_eval,
                    fetch_settings=rendered_fetch,
                    **request_nix_path,
                ),
            )
            proxy._eval_handle = response.eval_handle  # type: ignore[reportPrivateUsage] -- EvalSession owns its proxy's eval_handle assignment  # noqa: SLF001
        except BaseException:
            proxy.deactivate()
            releases.close()
            if self._owner_session is not None:
                self._owner_session.release_eval(self)
            raise
        self._active = [True]
        self._owner = _EvalOwner(_EvalOwnerToken(), self._active)
        self._releases = releases
        self._proxy = proxy
        self._ctx = _EvalProxyContext(proxy, self._owner, self._timeout, self._store_handle, self._session_id)
        if self._store is not None:
            self._store.rpc._register_dependent_eval(self)  # type: ignore[reportPrivateUsage] -- Store owns the public force-close lifecycle hook  # noqa: SLF001

    async def get_verbosity(self) -> LogLevel:
        """Return the current Nix log verbosity while this eval session is open."""
        self._ensure_proxy()
        return await self._worker.get_verbosity()

    async def set_verbosity(self, verbosity: LogLevelInput) -> LogLevel:
        """Update Nix log verbosity while retaining this eval session's scope."""
        self._ensure_proxy()
        return await self._worker.set_verbosity(normalize_log_level(verbosity))

    async def configure(
        self,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
    ) -> None:
        """Apply live eval and fetch settings to this already-open evaluator.

        Only the settings Nix reads fresh at the point of use can be changed
        this way, such as ``max_call_depth``, ``allowed_uris`` and the lint
        settings. Nix reads the others once, while it builds the evaluator, so
        this raises for those rather than accepting them and doing nothing.
        Every fetch setting is live.

        Raises:
            SettingNotLiveError: A setting Nix reads only at construction, such
                as ``nix_path``, ``pure_eval`` or ``restrict_eval``. Open a new
                evaluator with it: ``session.eval(store, eval_settings=...)``.
        """
        rendered_eval = eval_settings.to_worker_settings() if eval_settings is not None else {}
        rendered_fetch = fetch_settings.to_worker_settings() if fetch_settings is not None else {}
        reject_construction_time_keys(rendered_eval, model=NixEvalSettings, target="evaluator")
        reject_construction_time_keys(rendered_fetch, model=NixFetchSettings, target="evaluator")
        await self._ensure_proxy().configure_eval(
            ConfigureEvalRequest(eval_settings=rendered_eval, fetch_settings=rendered_fetch),
        )

    async def close(self) -> None:
        """Release all values and close this session's EvalState.

        Called automatically by ``async with``. Invalidates every
        ``ValueProxy`` exported from this session.
        """
        self._active[0] = False
        proxy = self._proxy
        if proxy is None:
            return
        try:
            try:
                await proxy.drain_deferred_releases()
            finally:
                await proxy.close_eval(CloseEvalRequest())
        finally:
            proxy.deactivate()
            self._proxy = None
            self._ctx = None
            releases = self._releases
            self._releases = None
            if releases is not None:
                releases.close()
            if self._store is not None:
                self._store.rpc._unregister_dependent_eval(self)  # type: ignore[reportPrivateUsage] -- Store owns the public force-close lifecycle hook  # noqa: SLF001
            if self._owner_session is not None:
                self._owner_session.release_eval(self)

    def _ensure_proxy(self) -> EvalProxy:
        p = self._proxy
        if p is None:
            raise EvalSessionClosedError("EvalSession not entered — use 'async with session.eval() as eval:'")
        return p

    def _proxy_context(self) -> _EvalProxyContext:
        ctx = self._ctx
        if ctx is None:
            raise EvalSessionClosedError("EvalSession not entered — use 'async with session.eval() as eval:'")
        return ctx

    def _deferred_releases(self) -> _DeferredReleases:
        releases = self._releases
        if releases is None:
            raise EvalSessionClosedError("EvalSession is not open — use 'async with session.eval() as eval:'")
        return releases

    async def file(self, path: str, *, timeout: float | None = None) -> ValueProxy:
        """Evaluate the Nix expression in the file at ``path``."""
        handle = await self._ensure_proxy().eval_file(EvalFileRequest(path=path))
        return self._proxy_context().value(handle.handle, handle.type)

    async def string(self, expr: str, path: str = "<string>", *, timeout: float | None = None) -> ValueProxy:
        """Evaluate the Nix expression ``expr``.

        Args:
            expr: Nix source to evaluate.
            path: Source name attributed to ``expr`` in error messages.
            timeout: Maximum seconds to wait for evaluation to complete. None
                (default) waits indefinitely.
        """
        handle = await self._ensure_proxy().eval_string(
            EvalStringRequest(expr=expr, source_name=path),
        )
        return self._proxy_context().value(handle.handle, handle.type)

    async def lock_flake(
        self,
        ref: str,
        *,
        update_inputs: bool | list[str] = False,
        write_lock_file: bool = True,
        flake_settings: NixFlakeSettings | None = None,
        timeout: float | None = None,
    ) -> LockedFlakeHandle:
        """Lock a flake, optionally updating inputs.

        Without update flags, creates missing lock entries only (like
        ``nix flake lock``).  With ``update_inputs=True``, re-resolves all
        inputs.  With ``update_inputs=["nixpkgs"]``, re-resolves only the
        specified inputs.

        Returns a session-bound ``LockedFlakeHandle``.  When
        ``write_lock_file=False``, the lock is updated in memory only; call
        ``await locked.write_lock_file()`` later to persist it.

        ``flake_settings`` is merged over this session's flake defaults, so a
        field named here wins and the rest come from ``Session(settings=...)``.
        """
        proxy = self._ensure_proxy()
        rendered_flake = merge_defaults(flake_settings, self._flake_defaults).to_worker_settings()
        if update_inputs is True:
            locked = await proxy.lock_flake(
                LockFlakeRequest(
                    ref=ref,
                    update_all=True,
                    write_lock_file=write_lock_file,
                    flake_settings=rendered_flake,
                ),
            )
        elif isinstance(update_inputs, list):
            locked = await proxy.lock_flake(
                LockFlakeRequest(
                    ref=ref,
                    update_inputs_list=UpdateInputsList(inputs=update_inputs),
                    write_lock_file=write_lock_file,
                    flake_settings=rendered_flake,
                ),
            )
        else:
            locked = await proxy.lock_flake(
                LockFlakeRequest(
                    ref=ref,
                    write_lock_file=write_lock_file,
                    flake_settings=rendered_flake,
                ),
            )
        return self._locked_flake_handle(locked)

    def _locked_flake_handle(self, locked: LockedFlakeProto) -> LockedFlakeHandle:
        return LockedFlakeHandle(
            self,
            handle=locked.handle,
            description=locked.description,
            inputs=locked.inputs,
        )

    def _locked_flake_id(self, locked: LockedFlakeHandle) -> int:
        if locked._session is not self:  # type: ignore[reportPrivateUsage] -- cross-class access  # noqa: SLF001
            raise ForeignValueError("cannot use a LockedFlakeHandle from another EvalSession")
        return locked.handle

    async def _eval_locked_flake(self, locked: LockedFlakeHandle, *, timeout: float | None = None) -> ValueProxy:
        """Evaluate a previously locked flake by handle. Backs ``LockedFlakeHandle.eval``.

        Private: ``LockedFlakeHandle.eval()`` is the door, and it is the door
        on both engines -- ``inproc.LockedFlake`` has the same method and no
        session-level twin, because in-process there is no handle to pass. The
        public pair here was duplicate surface, and the signature ledger
        counted it as a DEFECT for exactly that reason.
        """
        result = await self._ensure_proxy().call_locked_flake(
            CallLockedFlakeRequest(handle=self._locked_flake_id(locked)),
        )
        return self._proxy_context().value(result.handle, result.type)

    async def _write_lock_file(self, locked: LockedFlakeHandle, *, timeout: float | None = None) -> None:
        """Write a locked flake's lock file to disk. Backs ``LockedFlakeHandle.write_lock_file``.

        Persists the in-memory lock from a prior ``lock_flake(write_lock_file=False)``
        call.  The lock file is written to the flake's own directory — for a
        temp flake this is the temp directory, never a real path.

        Private for the same reason as :meth:`_eval_locked_flake`.
        """
        await self._ensure_proxy().write_lock_file(WriteLockFileRequest(handle=self._locked_flake_id(locked)))

    async def release_locked_flake(self, locked: LockedFlakeHandle, *, timeout: float | None = None) -> None:
        """Release the worker-side handle for a locked flake. Prefer ``locked.release()``."""
        await self._ensure_proxy().release_locked_flake(ReleaseLockedFlakeRequest(handle=self._locked_flake_id(locked)))

    async def eval_flake(
        self,
        ref: str,
        *,
        write_lock_file: bool = True,
        flake_settings: NixFlakeSettings | None = None,
        timeout: float | None = None,
    ) -> ValueProxy:
        """Lock and evaluate a flake in one step, returning its outputs as a ``ValueProxy``.

        Equivalent to ``nix eval <ref>#`` — calls ``lockFlake`` then
        ``callFlake`` and returns the outputs attrset.  Navigate with
        ``.attr()``, ``.to_python()``, etc.

        For more control (e.g. updating locks in memory before evaluating),
        use ``lock_flake()`` + ``locked.eval()`` instead.

        ``flake_settings`` is merged over this session's flake defaults, as in
        :meth:`lock_flake`.
        """
        handle = await self._ensure_proxy().eval_flake(
            EvalFlakeRequest(
                ref=ref,
                write_lock_file=write_lock_file,
                flake_settings=merge_defaults(flake_settings, self._flake_defaults).to_worker_settings(),
            ),
        )
        return self._proxy_context().value(handle.handle, handle.type)

    async def get_flake(self, ref: str | dict[str, Any], *, timeout: float | None = None) -> FlakeRef:
        """Parse and resolve a flake reference without evaluating its outputs."""
        ref_str = ref if isinstance(ref, str) else str(ref)
        return await self._ensure_proxy().get_flake(GetFlakeRequest(ref=ref_str))

    async def reset_file_cache(self, *, timeout: float | None = None) -> None:
        """Discard parsed file cache entries before re-evaluating source files."""
        await self._ensure_proxy().reset_file_cache(ResetFileCacheRequest())


class ReplSession(EvalSession):
    """An :class:`EvalSession` with a persistent Nix lexical scope.

    Bindings submitted with :meth:`line` are available to later calls to
    :meth:`string` and :meth:`file` until this context manager closes.
    """

    @property
    def line_editors(self) -> tuple[str, ...]:
        """Editor-name substrings that support Nix's ``+LINE`` argument."""
        return self._line_editors

    async def __aenter__(self) -> ReplSession:
        await self.open()
        return self

    async def open(self) -> None:
        """Open the evaluator, then begin its persistent REPL scope.

        Idempotent, because :meth:`EvalSession.open` is. The worker's
        ``begin_repl`` raises on a second call rather than no-opping, so
        without the flag a harmless double ``open()`` would surface an opaque
        "REPL scope is already active" error from the bindings.
        """
        await super().open()
        if self._repl_begun:
            return
        try:
            await self._ensure_proxy().begin_repl(BeginReplRequest())
        except BaseException:
            await self.close()
            raise
        self._repl_begun = True

    async def close(self) -> None:
        """Close the evaluator; a later :meth:`open` begins a fresh REPL scope.

        The REPL env belongs to the worker's ``EvalState``, so closing discards
        it and reopening must call ``begin_repl`` again.
        """
        self._repl_begun = False
        await super().close()

    async def line(self, text: str, path: str = "<string>", *, timeout: float | None = None) -> ValueProxy | None:
        """Process one Nix REPL line.

        A binding such as ``x = 1`` returns ``None``. An expression returns a
        session-bound :class:`ValueProxy`.
        """
        response = await self._ensure_proxy().repl_process_line(
            ReplProcessLineRequest(line=text, source_name=path),
        )
        if response.is_binding:
            return None
        value = response.value
        if value is None:
            raise RuntimeError("worker returned an expression result without a value handle")
        return self._proxy_context().value(value.handle, value.type)

    async def load_file(self, path: str, *, timeout: float | None = None) -> ValueProxy:
        """Load a Nix expression file as ``nix repl :load`` does.

        Nix auto-calls a top-level function with its default arguments before
        returning the resulting attribute set for :meth:`add_attrs`.
        """
        handle = await self._ensure_proxy().repl_load_file(
            ReplLoadFileRequest(path=path),
        )
        return self._proxy_context().value(handle.handle, handle.type)

    async def add_attrs(self, value: ValueProxy, *, timeout: float | None = None) -> list[str]:
        """Add all attributes from ``value`` to this REPL's lexical scope."""
        if not self._owner.owns(value):
            raise ForeignValueError("cannot add attributes from another EvalSession")
        await value._ensure_resolved(timeout=timeout)  # type: ignore[reportPrivateUsage] -- session owns the value context  # noqa: SLF001
        response = await self._ensure_proxy().repl_add_attrs(ReplAddAttrsRequest(handle=value.handle))
        return response.names

    async def scope_names(self, *, timeout: float | None = None) -> list[str]:
        """Return the identifiers visible in this REPL's lexical scope."""
        response = await self._ensure_proxy().repl_scope_names(ReplScopeNamesRequest())
        return response.names
