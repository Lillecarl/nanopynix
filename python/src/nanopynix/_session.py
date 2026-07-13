# ruff: noqa: ASYNC109
"""Eval session — exclusive worker lock + ValueProxy for eval over gRPC."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import TYPE_CHECKING, Any, Literal, overload

from nanopynix_proto.nix.common import (
    CallArg,
    CallArgAttrs,
    CallArgList,
    DeepValue,
    ForceValue,
    NullValue,
    RemoteCallArg,
    ScalarValue,
)
from nanopynix_proto.nix.eval import EvalServiceBase

from nanopynix._pool import _RPC_TIMEOUT
from nanopynix._rpc_proxy import RpcProxyMixin
from nanopynix.exceptions import (
    EvalSessionClosedError,
    ForeignValueError,
    NixCoercionError,
    StoreError,
    UnresolvedValueError,
    ValueReleasedError,
    WrongNixTypeError,
)
from nanopynix.models import FlakeRef, JsonScalar, JsonValue, LockedInput, NixType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from betterproto2 import Message
    from nanopynix_proto.nix.common import LockedFlake as LockedFlakeProto

    from nanopynix._pool import ReservedWorker, _WorkerManager
    from nanopynix.store import StoreHandle
    from nanopynix_store import BuildMode as BuildModeType


def _scalar_to_pyval(scalar: ScalarValue | None) -> JsonScalar:
    """Convert a proto ScalarValue to a Python JSON-scalar."""
    if scalar is None:
        return None
    if scalar.string_value is not None:
        return scalar.string_value
    if scalar.int_value is not None:
        return scalar.int_value
    if scalar.float_value is not None:
        return scalar.float_value
    if scalar.bool_value is not None:
        return scalar.bool_value
    return None  # null_value


def _pyval_to_scalar(v: JsonScalar) -> ScalarValue:
    """Convert a Python JSON-scalar value to a proto ScalarValue."""
    if v is None:
        return ScalarValue(null_value=NullValue())
    if isinstance(v, bool):
        return ScalarValue(bool_value=v)
    if isinstance(v, int):
        return ScalarValue(int_value=v)
    if isinstance(v, float):
        return ScalarValue(float_value=v)
    return ScalarValue(string_value=str(v))


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


@dataclass(frozen=True)
class _EvalOwnerToken:
    pass


@dataclass(frozen=True)
class _LazyValue:
    parent: ValueProxy | _ResolvedValue
    selector: str | int
    nix_type: NixType | None = None


type _ValueState = _ResolvedValue | _LazyValue
type _ActiveFlag = list[bool]


@dataclass(frozen=True)
class _EvalOwner:
    token: _EvalOwnerToken
    active: _ActiveFlag | None = None

    def owns(self, value: ValueProxy) -> bool:
        return value._ctx.owner.token is self.token


# ════════════════════════════════════════════════════════════════════
# EvalProxy — auto-generated RPC proxy for the EvalService gRPC API
# ════════════════════════════════════════════════════════════════════


class EvalProxy(RpcProxyMixin, EvalServiceBase, rpc_service_base=EvalServiceBase):
    """Auto-generated proxy for all 20 ``EvalService`` gRPC methods.

    Created by ``EvalSession.open()`` when the worker is reserved.
    Method forwarders dispatch through ``ReservedWorker.call()``,
    serializing all eval RPCs under the worker's exclusive lease.
    """

    __slots__ = ("_active", "_rw")

    def __init__(self, rw: ReservedWorker) -> None:
        self._rw = rw
        self._active = True

    def _check_active(self) -> None:
        if not self._active:
            raise EvalSessionClosedError("EvalProxy is invalid — the EvalSession has been closed")

    def deactivate(self) -> None:
        self._active = False

    async def _rpc_proxy_call(self, method_name: str, message: Message) -> Any:
        self._check_active()
        method = getattr(self._rw._eval_stub, method_name)
        return await self._rw.call(method(message, timeout=_RPC_TIMEOUT))

    async def _store_proxy_call(self, method_name: str, message: Message) -> Any:
        self._check_active()
        method = getattr(self._rw._store_stub, method_name)
        return await self._rw.call(method(message, timeout=_RPC_TIMEOUT))


@dataclass(frozen=True)
class _EvalProxyContext:
    proxy: EvalProxy
    owner: _EvalOwner
    timeout: float | None = None
    store_handle: int = 1
    session_id: str = ""

    def with_timeout(self, timeout: float | None) -> _EvalProxyContext:
        if timeout is None:
            return self
        return _EvalProxyContext(self.proxy, self.owner, timeout, self.store_handle, self.session_id)

    def resolve_timeout(self, override: float | None) -> float | None:
        return override if override is not None else self.timeout

    def value(self, handle: int, typ: Any) -> ValueProxy:
        return ValueProxy(self, _ResolvedValue(handle=handle, nix_type=_parse_nix_type(typ)))

    def child(
        self,
        parent: ValueProxy | _ResolvedValue,
        selector: str | int,
        *,
        timeout: float | None = None,
    ) -> ValueProxy:
        return ValueProxy(
            self.with_timeout(self.resolve_timeout(timeout)),
            _LazyValue(parent=parent, selector=selector),
        )

    def attrs(self, handle: int, keys: Sequence[str]) -> ValueAttrs:
        return ValueAttrs(self, _ResolvedValue(handle=handle, nix_type=NixType.ATTRS), keys)

    def list(self, handle: int, length: int) -> ValueList:
        return ValueList(self, _ResolvedValue(handle=handle, nix_type=NixType.LIST), length)


type NixArg = ValueProxy | JsonScalar | list[NixArg] | dict[str, NixArg]
type NixValue = ValueProxy | ValueAttrs | ValueList | JsonValue
type NixDeepValue = ValueProxy | JsonScalar | list[NixDeepValue] | dict[str, NixDeepValue]


class ValueProxy:
    """Proxy for a Nix Value exported on the remote worker.

    Lifetime is tied to the ``EvalSession`` that created it — all gRPC
    methods raise ``EvalSessionClosedError`` after the session exits.

    Supports ``async with`` for early release.
    """

    __slots__ = (
        "_ctx",
        "_released",
        "_state",
    )

    def __init__(self, ctx: _EvalProxyContext, state: _ValueState) -> None:
        self._ctx = ctx
        self._state = state
        self._released = False

    async def __aenter__(self) -> ValueProxy:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

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
    def nix_type(self) -> NixType:
        return self._state.nix_type or NixType.UNSPECIFIED

    async def _ensure_resolved(self, *, timeout: float | None = None) -> None:
        self._check_active()
        if isinstance(self._state, _ResolvedValue):
            return
        lazy = self._state
        if isinstance(lazy.parent, ValueProxy):
            await lazy.parent._ensure_resolved(timeout=timeout)
            parent = lazy.parent._resolved
        else:
            parent = lazy.parent
        if isinstance(lazy.selector, str):
            from nanopynix_proto.nix.eval import AttrRequest

            handle = await self._ctx.proxy.attr(AttrRequest(handle=parent.handle, name=lazy.selector))
        else:
            from nanopynix_proto.nix.eval import ListGetRequest

            handle = await self._ctx.proxy.list_get(ListGetRequest(handle=parent.handle, index=lazy.selector))
        self._state = _ResolvedValue(handle=handle.handle, nix_type=_parse_nix_type(handle.type))

    async def _ensure_type(self, *, timeout: float | None = None) -> NixType:
        await self._ensure_resolved(timeout=timeout)
        cached = self._state.nix_type
        if cached not in (None, NixType.THUNK, NixType.UNSPECIFIED):
            return cached
        from nanopynix_proto.nix.eval import TypeNameRequest

        resp = await self._ctx.proxy.type_name(TypeNameRequest(handle=self.handle))
        resolved_type = _parse_nix_type(resp.type) or NixType.UNSPECIFIED
        self._state = _ResolvedValue(handle=self.handle, nix_type=resolved_type)
        return resolved_type

    def _decode_force_value(self, value: ForceValue) -> JsonValue | ValueProxy:
        if value.remote_value is not None:
            return self._ctx.value(value.remote_value.handle, value.remote_value.type)
        return _scalar_to_pyval(value.scalar)

    def _decode_deep_value(self, value: DeepValue) -> NixDeepValue:
        if value.remote_value is not None:
            return self._ctx.value(value.remote_value.handle, value.remote_value.type)
        if value.scalar is not None:
            return _scalar_to_pyval(value.scalar)
        if value.list is not None:
            return [self._decode_deep_value(item) for item in value.list.items]
        if value.attrs is not None:
            return {key: self._decode_deep_value(item) for key, item in value.attrs.entries.items()}
        raise TypeError(f"unsupported force_deep RPC value: {value!r}")

    async def _encode_call_arg(self, value: NixArg, *, timeout: float | None) -> CallArg:
        if isinstance(value, ValueProxy):
            if not self._ctx.owner.owns(value):
                raise ForeignValueError("cannot pass a ValueProxy from another EvalSession")
            await value._ensure_resolved(timeout=timeout)
            return CallArg(remote_value=RemoteCallArg(handle=value.handle))
        if isinstance(value, list):
            return CallArg(
                list=CallArgList(items=[await self._encode_call_arg(item, timeout=timeout) for item in value])
            )
        if isinstance(value, dict):
            return CallArg(
                attrs=CallArgAttrs(
                    entries={key: await self._encode_call_arg(item, timeout=timeout) for key, item in value.items()}
                )
            )
        return CallArg(scalar=_pyval_to_scalar(value))

    # ── force ──────────────────────────────────────────────────────

    async def force(self, *, timeout: float | None = None) -> NixValue:
        """Evaluate to WHNF.  Compound types return lazy wrappers."""
        typ = await self._ensure_type(timeout=timeout)
        if typ == NixType.ATTRS:
            keys = await self.attr_names(timeout=timeout)
            return self._ctx.attrs(self.handle, keys)
        if typ == NixType.LIST:
            length = await self.list_length(timeout=timeout)
            return self._ctx.list(self.handle, length)
        if typ == NixType.FUNCTION:
            return self
        # scalar — delegate to worker
        from nanopynix_proto.nix.eval import ForceRequest

        result = await self._ctx.proxy.force(ForceRequest(handle=self.handle))
        return self._decode_force_value(result)

    @overload
    async def force_as(self, typ: Literal[NixType.INT], *, timeout: float | None = None) -> int: ...
    @overload
    async def force_as(self, typ: Literal[NixType.FLOAT], *, timeout: float | None = None) -> float: ...
    @overload
    async def force_as(self, typ: Literal[NixType.BOOL], *, timeout: float | None = None) -> bool: ...
    @overload
    async def force_as(self, typ: Literal[NixType.STRING], *, timeout: float | None = None) -> str: ...
    @overload
    async def force_as(self, typ: Literal[NixType.PATH], *, timeout: float | None = None) -> str: ...
    @overload
    async def force_as(self, typ: Literal[NixType.NULL], *, timeout: float | None = None) -> None: ...
    @overload
    async def force_as(self, typ: Literal[NixType.ATTRS], *, timeout: float | None = None) -> ValueAttrs: ...
    @overload
    async def force_as(self, typ: Literal[NixType.LIST], *, timeout: float | None = None) -> ValueList: ...
    @overload
    async def force_as(self, typ: Literal[NixType.FUNCTION], *, timeout: float | None = None) -> ValueProxy: ...
    async def force_as(self, typ: NixType, *, timeout: float | None = None) -> NixValue:
        actual = await self._ensure_type(timeout=timeout)
        if actual != typ:
            raise WrongNixTypeError(expected=typ, actual=actual)
        return await self.force(timeout=timeout)

    async def coerce_str(self, *, timeout: float | None = None) -> str:
        value = await self.force(timeout=timeout)
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int | float):
            return str(value)
        if value is None:
            return "null"
        raise NixCoercionError(f"cannot coerce Nix {self.nix_type.name.lower()} to string")

    async def coerce_int(self, *, timeout: float | None = None) -> int:
        value = await self.force(timeout=timeout)
        if isinstance(value, bool):
            raise NixCoercionError("cannot coerce Nix bool to int")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            raise NixCoercionError(f"cannot coerce non-integral float {value!r} to int")
        if isinstance(value, str):
            text = value.strip()
            if text and text.lstrip("+-").isdigit():
                return int(text, 10)
            raise NixCoercionError(f"cannot coerce string {value!r} to int")
        raise NixCoercionError(f"cannot coerce Nix {self.nix_type.name.lower()} to int")

    async def coerce_float(self, *, timeout: float | None = None) -> float:
        value = await self.force(timeout=timeout)
        if isinstance(value, bool):
            raise NixCoercionError("cannot coerce Nix bool to float")
        if isinstance(value, int | float):
            result = float(value)
        elif isinstance(value, str):
            try:
                result = float(value.strip())
            except ValueError as exc:
                raise NixCoercionError(f"cannot coerce string {value!r} to float") from exc
        else:
            raise NixCoercionError(f"cannot coerce Nix {self.nix_type.name.lower()} to float")
        if not isfinite(result):
            raise NixCoercionError(f"cannot coerce non-finite value {value!r} to float")
        return result

    async def coerce_bool(self, *, timeout: float | None = None) -> bool:
        value = await self.force(timeout=timeout)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text == "true":
                return True
            if text == "false":
                return False
            raise NixCoercionError(f"cannot coerce string {value!r} to bool")
        raise NixCoercionError(f"cannot coerce Nix {self.nix_type.name.lower()} to bool")

    async def force_deep(self, *, timeout: float | None = None) -> NixDeepValue:
        """Recursive Nix force. Functions remain remote callable ValueProxy objects."""
        await self._ensure_resolved(timeout=timeout)
        from nanopynix_proto.nix.eval import ForceDeepRequest

        result = await self._ctx.proxy.force_deep(ForceDeepRequest(handle=self.handle))
        return self._decode_deep_value(result)

    async def force_json(self, *, copy_to_store: bool = False, timeout: float | None = None) -> JsonValue:
        """Serialize the Nix value to JSON-compatible Python objects in one C++ pass.

        Uses Nix's ``printValueAsJSON`` with ``strict=true``: attrsets with a
        ``__toString`` attribute are coerced to its string result, attrsets with
        an ``outPath`` attribute (e.g. derivations) are serialized to that store
        path string, and everything else is recursively forced and converted.
        Avoids the cyclic-``all`` stack overflow that ``force_deep`` hits on
        derivations, and avoids per-value RPC round-trips.

        Args:
            copy_to_store: If True, ``path`` values are copied into the Nix store
                and rendered as store paths. If False (default), paths are
                rendered as literal filesystem paths.
        """
        await self._ensure_resolved(timeout=timeout)
        from nanopynix_proto.nix.eval import ForceJsonRequest

        resp = await self._ctx.proxy.force_json(
            ForceJsonRequest(handle=self.handle, copy_to_store=copy_to_store),
        )
        import json as _json

        return _json.loads(resp.json)

    async def build(
        self,
        *,
        build_mode: BuildModeType | int | None = None,
        store: StoreHandle | None = None,
        timeout: float | None = None,
    ) -> dict[str, str]:
        """Build the derivation represented by this evaluated value."""
        build_store_handle = self._build_store_handle(store)
        drv_path = await self._derivation_drv_path(timeout=timeout)
        mode_value = self._build_mode_value(build_mode)
        await self._build_derivation_path(drv_path, mode_value, build_store_handle)
        return await self._read_derivation_outputs(drv_path)

    def _build_store_handle(self, store: StoreHandle | None) -> int:
        if store is None:
            return self._ctx.store_handle
        if store._session_id != self._ctx.session_id:
            raise ValueError("StoreHandle belongs to a different session")
        return store.store_handle

    async def _derivation_drv_path(self, *, timeout: float | None) -> str:
        if not await self.has_attr("type", timeout=timeout):
            raise TypeError("nix value is not a derivation")
        value_type = await self.attr("type", timeout=timeout).force_json(timeout=timeout)
        if value_type != "derivation":
            raise TypeError("nix value is not a derivation")
        drv_path = await self.attr("drvPath", timeout=timeout).force_json(timeout=timeout)
        if isinstance(drv_path, str):
            return drv_path
        actual_type = await self.attr("drvPath").get_type(timeout=timeout)
        raise WrongNixTypeError(expected=NixType.STRING, actual=actual_type)

    def _build_mode_value(self, build_mode: BuildModeType | int | None) -> int:
        from nanopynix_store import BuildMode

        if build_mode is None:
            return BuildMode.Normal.value
        if isinstance(build_mode, int):
            return build_mode
        return build_mode.value

    async def _build_derivation_path(self, drv_path: str, build_mode: int, build_store_handle: int) -> None:
        from nanopynix_proto.nix.store import (
            BuildPathsWithResultsRequest,
        )

        eval_store_handle = 0 if build_store_handle == self._ctx.store_handle else self._ctx.store_handle
        build_results = await self._ctx.proxy._store_proxy_call(
            "build_for_humans",
            BuildPathsWithResultsRequest(
                paths=[drv_path],
                build_mode=build_mode,
                eval_store_handle=eval_store_handle,
                store_handle=build_store_handle,
            ),
        )
        if not build_results.results:
            raise StoreError("StoreError", f"build returned no result for derivation {drv_path}")
        result = build_results.results[0]
        if not result.success:
            msg = result.error_msg or f"failed to build derivation {drv_path}"
            raise StoreError("StoreError", msg)

    async def _read_derivation_outputs(self, drv_path: str) -> dict[str, str]:
        from nanopynix_proto.nix.store import ReadDerivationRequest

        derivation = await self._ctx.proxy._store_proxy_call(
            "read_derivation",
            ReadDerivationRequest(path=drv_path, store_handle=self._ctx.store_handle),
        )

        outputs: dict[str, str] = {}
        for output, output_info in derivation.outputs.items():
            path = output_info.path
            if path is None:
                raise StoreError("StoreError", f"derivation output {output!r} has no path")
            outputs[output] = path
        if outputs:
            return outputs

        for output in derivation.env.get("outputs", "out").split():
            path = derivation.env.get(output)
            if path is None:
                raise StoreError("StoreError", f"derivation output {output!r} has no path")
            outputs[output] = path
        return outputs

    # ── navigation ─────────────────────────────────────────────────

    def attr(self, name: str, *, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        parent: ValueProxy | _ResolvedValue = self if isinstance(self._state, _LazyValue) else self._resolved
        return self._ctx.child(parent, name, timeout=timeout)

    def list_get(self, idx: int, *, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        parent: ValueProxy | _ResolvedValue = self if isinstance(self._state, _LazyValue) else self._resolved
        return self._ctx.child(parent, idx, timeout=timeout)

    async def list_length(self, *, timeout: float | None = None) -> int:
        await self._ensure_resolved(timeout=timeout)
        from nanopynix_proto.nix.eval import ListLengthRequest

        resp = await self._ctx.proxy.list_length(ListLengthRequest(handle=self.handle))
        return resp.length

    async def attr_names(self, *, timeout: float | None = None) -> list[str]:
        await self._ensure_resolved(timeout=timeout)
        from nanopynix_proto.nix.eval import AttrNamesRequest

        resp = await self._ctx.proxy.attr_names(AttrNamesRequest(handle=self.handle))
        return resp.names

    async def has_attr(self, name: str, *, timeout: float | None = None) -> bool:
        await self._ensure_resolved(timeout=timeout)
        from nanopynix_proto.nix.eval import HasAttrRequest

        resp = await self._ctx.proxy.has_attr(HasAttrRequest(handle=self.handle, name=name))
        return resp.has

    async def call(self, *args: NixArg, timeout: float | None = None) -> ValueProxy:
        await self._ensure_resolved(timeout=timeout)
        actual = await self._ensure_type(timeout=timeout)
        if actual != NixType.FUNCTION:
            raise WrongNixTypeError(expected=NixType.FUNCTION, actual=actual)
        call_args = [await self._encode_call_arg(arg, timeout=timeout) for arg in args]
        from nanopynix_proto.nix.eval import CallRequest

        result = await self._ctx.proxy.call(CallRequest(handle=self.handle, args=call_args))
        return self._ctx.value(result.handle, result.type)

    async def auto_call(self, *, timeout: float | None = None) -> ValueProxy:
        """Apply Nix top-level auto-call semantics to a function value."""
        await self._ensure_resolved(timeout=timeout)
        from nanopynix_proto.nix.eval import AutoCallRequest

        result = await self._ctx.proxy.auto_call(AutoCallRequest(handle=self.handle))
        return self._ctx.value(result.handle, result.type)

    async def __call__(self, *args: NixArg, timeout: float | None = None) -> ValueProxy:
        return await self.call(*args, timeout=timeout)

    async def get_type(self, *, timeout: float | None = None) -> NixType:
        return await self._ensure_type(timeout=timeout)

    # ── release ────────────────────────────────────────────────────

    async def release(self, *, timeout: float | None = None) -> None:
        if not isinstance(self._state, _ResolvedValue):
            self._released = True
            return
        self._check_active()
        from nanopynix_proto.nix.eval import ReleaseRequest

        await self._ctx.proxy.release(ReleaseRequest(handle=self.handle))
        self._released = True


# ════════════════════════════════════════════════════════════════════
# _ChildProxy — lazy proxy for attrset attributes / list elements
# ════════════════════════════════════════════════════════════════════

_ChildProxy = ValueProxy


# ════════════════════════════════════════════════════════════════════
# ValueAttrs — lazy attrset (keys accessible, values lazy)
# ════════════════════════════════════════════════════════════════════


class ValueAttrs:
    """Attrset forced to WHNF — keys are available, values are still lazy.

    ``__getitem__`` returns a ``ValueProxy``.  ``__aenter__``/``__aexit__``
    support early release of the underlying handle.
    """

    __slots__ = ("_ctx", "_keys", "_released", "_value")

    def __init__(
        self,
        ctx: _EvalProxyContext,
        value: _ResolvedValue,
        keys: Sequence[str],
    ) -> None:
        self._ctx = ctx
        self._value = value
        self._keys = keys
        self._released = False

    async def __aenter__(self) -> ValueAttrs:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

    def _check_active(self) -> None:
        active = self._ctx.owner.active
        if active is not None and not active[0]:
            raise EvalSessionClosedError("ValueAttrs is invalid — the EvalSession has been closed")
        if self._released:
            raise ValueReleasedError("ValueAttrs has been released")

    def keys(self) -> list[str]:
        return list(self._keys)

    def __getitem__(self, name: str) -> ValueProxy:
        """Return a lazy child proxy — the RPC fires on ``await .force()``."""
        self._check_active()
        return self._ctx.child(self._value, name)

    async def force(self, name: str, *, timeout: float | None = None) -> NixValue:
        """Force a single attribute and return its value."""
        self._check_active()
        from nanopynix_proto.nix.eval import AttrRequest

        result = await self._ctx.proxy.attr(AttrRequest(handle=self._value.handle, name=name))
        proxy = self._ctx.value(result.handle, result.type)
        return await proxy.force()

    async def release(self) -> None:
        self._check_active()
        from nanopynix_proto.nix.eval import ReleaseRequest

        await self._ctx.proxy.release(ReleaseRequest(handle=self._value.handle))
        self._released = True


# ════════════════════════════════════════════════════════════════════
# ValueList — lazy list (length accessible, elements lazy)
# ════════════════════════════════════════════════════════════════════


class ValueList:
    """List forced to WHNF — length is available, elements are still lazy.

    ``__getitem__`` returns a ``ValueProxy``.  ``__aenter__``/``__aexit__``
    support early release of the underlying handle.
    """

    __slots__ = ("_ctx", "_length", "_released", "_value")

    def __init__(
        self,
        ctx: _EvalProxyContext,
        value: _ResolvedValue,
        length: int,
    ) -> None:
        self._ctx = ctx
        self._value = value
        self._length = length
        self._released = False

    async def __aenter__(self) -> ValueList:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

    def _check_active(self) -> None:
        active = self._ctx.owner.active
        if active is not None and not active[0]:
            raise EvalSessionClosedError("ValueList is invalid — the EvalSession has been closed")
        if self._released:
            raise ValueReleasedError("ValueList has been released")

    def _check_index(self, idx: int) -> None:
        normalized = idx if idx >= 0 else idx + self._length
        if normalized < 0 or normalized >= self._length:
            raise IndexError(f"list index {idx} out of range for length {self._length}")

    def __len__(self) -> int:
        return self._length

    def __getitem__(self, idx: int) -> ValueProxy:
        self._check_active()
        self._check_index(idx)
        return self._ctx.child(self._value, idx)

    async def force(self, idx: int, *, timeout: float | None = None) -> NixValue:
        """Force a single element and return its value."""
        self._check_active()
        self._check_index(idx)
        from nanopynix_proto.nix.eval import ListGetRequest

        result = await self._ctx.proxy.list_get(ListGetRequest(handle=self._value.handle, index=idx))
        proxy = self._ctx.value(result.handle, result.type)
        return await proxy.force()

    async def release(self) -> None:
        self._check_active()
        from nanopynix_proto.nix.eval import ReleaseRequest

        await self._ctx.proxy.release(ReleaseRequest(handle=self._value.handle))
        self._released = True


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

    def _check_active(self) -> None:
        if self._released:
            raise ValueReleasedError("LockedFlakeHandle has been released")

    async def eval(self, *, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        return await self._session.eval_locked_flake(self, timeout=timeout)

    async def write_lock_file(self, *, timeout: float | None = None) -> None:
        self._check_active()
        await self._session.write_lock_file(self, timeout=timeout)

    async def release(self, *, timeout: float | None = None) -> None:
        if self._released:
            return
        await self._session.release_locked_flake(self, timeout=timeout)
        self._released = True


class EvalSession:
    """Holds the worker exclusively for the duration of an eval session.

    All ``ValueProxy`` instances created through this session become
    invalid after ``__aexit__`` — their RPC methods raise ``EvalSessionClosedError``.
    """

    __slots__ = ("_active", "_ctx", "_manager", "_owner", "_proxy", "_rw", "_session_id", "_store_handle", "_timeout")

    def __init__(
        self,
        manager: _WorkerManager,
        store_handle: int = 1,
        timeout: float | None = None,
        session_id: str = "",
    ) -> None:
        self._manager = manager
        self._store_handle = store_handle
        self._session_id = session_id
        self._rw: ReservedWorker | None = None
        self._proxy: EvalProxy | None = None
        self._timeout = timeout
        self._active: list[bool] = [False]
        self._owner = _EvalOwner(_EvalOwnerToken(), self._active)
        self._ctx: _EvalProxyContext | None = None

    async def __aenter__(self) -> EvalSession:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._rw is not None:
            return
        self._rw = await self._manager.reserve(timeout=self._timeout)
        self._proxy = EvalProxy(self._rw)
        self._ctx = _EvalProxyContext(
            self._proxy,
            self._owner,
            self._timeout,
            self._store_handle,
            self._session_id,
        )
        self._active[0] = True

    async def close(self) -> None:
        self._active[0] = False
        if self._rw is not None:
            try:
                from nanopynix_proto.nix.eval import ReleaseAllRequest

                await self._proxy.release_all(ReleaseAllRequest())  # type: ignore[union-attr]
            finally:
                self._proxy.deactivate()  # type: ignore[union-attr]
                await self._rw.release()
                self._rw = None
                self._proxy = None
                self._ctx = None

    def _ensure_proxy(self) -> EvalProxy:
        p = self._proxy
        if p is None:
            raise EvalSessionClosedError("EvalSession not entered — use 'async with session.eval() as eval_:'")
        return p

    def _proxy_context(self) -> _EvalProxyContext:
        ctx = self._ctx
        if ctx is None:
            raise EvalSessionClosedError("EvalSession not entered — use 'async with session.eval() as eval_:'")
        return ctx

    async def file(self, path: str, *, timeout: float | None = None) -> ValueProxy:
        from nanopynix_proto.nix.eval import EvalFileRequest

        handle = await self._ensure_proxy().eval_file(EvalFileRequest(path=path, store_handle=self._store_handle))
        return self._proxy_context().value(handle.handle, handle.type)

    async def string(self, expr: str, path: str = "<string>", *, timeout: float | None = None) -> ValueProxy:
        from nanopynix_proto.nix.eval import EvalStringRequest

        handle = await self._ensure_proxy().eval_string(
            EvalStringRequest(expr=expr, source_name=path, store_handle=self._store_handle)
        )
        return self._proxy_context().value(handle.handle, handle.type)

    async def lock_flake(
        self,
        ref: str,
        *,
        update_inputs: bool | list[str] = False,
        write_lock_file: bool = True,
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
        """
        from nanopynix_proto.nix.eval import LockFlakeRequest, UpdateInputsList

        proxy = self._ensure_proxy()
        if update_inputs is True:
            locked = await proxy.lock_flake(
                LockFlakeRequest(
                    ref=ref,
                    update_all=True,
                    write_lock_file=write_lock_file,
                    store_handle=self._store_handle,
                )
            )
        elif isinstance(update_inputs, list):
            locked = await proxy.lock_flake(
                LockFlakeRequest(
                    ref=ref,
                    update_inputs_list=UpdateInputsList(inputs=update_inputs),
                    write_lock_file=write_lock_file,
                    store_handle=self._store_handle,
                )
            )
        else:
            locked = await proxy.lock_flake(
                LockFlakeRequest(
                    ref=ref,
                    write_lock_file=write_lock_file,
                    store_handle=self._store_handle,
                )
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
        if locked._session is not self:
            raise ForeignValueError("cannot use a LockedFlakeHandle from another EvalSession")
        return locked.handle

    async def eval_locked_flake(self, locked: LockedFlakeHandle, *, timeout: float | None = None) -> ValueProxy:
        """Evaluate a previously locked flake by handle.

        Calls the flake's ``outputs`` function using the in-memory
        ``LockedFlake`` from a prior ``lock_flake()`` call.  This allows
        evaluating with an updated lock that hasn't been written to disk yet.
        """
        from nanopynix_proto.nix.eval import CallLockedFlakeRequest

        result = await self._ensure_proxy().call_locked_flake(
            CallLockedFlakeRequest(handle=self._locked_flake_id(locked))
        )
        return self._proxy_context().value(result.handle, result.type)

    async def write_lock_file(self, locked: LockedFlakeHandle, *, timeout: float | None = None) -> None:
        """Write a locked flake's lock file to disk.

        Persists the in-memory lock from a prior ``lock_flake(write_lock_file=False)``
        call.  The lock file is written to the flake's own directory — for a
        temp flake this is the temp directory, never a real path.
        """
        from nanopynix_proto.nix.eval import WriteLockFileRequest

        await self._ensure_proxy().write_lock_file(WriteLockFileRequest(handle=self._locked_flake_id(locked)))

    async def release_locked_flake(self, locked: LockedFlakeHandle, *, timeout: float | None = None) -> None:
        from nanopynix_proto.nix.eval import ReleaseLockedFlakeRequest

        await self._ensure_proxy().release_locked_flake(ReleaseLockedFlakeRequest(handle=self._locked_flake_id(locked)))

    async def eval_flake(
        self,
        ref: str,
        *,
        write_lock_file: bool = True,
        timeout: float | None = None,
    ) -> ValueProxy:
        """Lock and evaluate a flake in one step, returning its outputs as a ``ValueProxy``.

        Equivalent to ``nix eval <ref>#`` — calls ``lockFlake`` then
        ``callFlake`` and returns the outputs attrset.  Navigate with
        ``.attr()``, ``.force_json()``, etc.

        For more control (e.g. updating locks in memory before evaluating),
        use ``lock_flake()`` + ``eval_locked_flake()`` instead.
        """
        from nanopynix_proto.nix.eval import EvalFlakeRequest

        handle = await self._ensure_proxy().eval_flake(
            EvalFlakeRequest(
                ref=ref,
                write_lock_file=write_lock_file,
                store_handle=self._store_handle,
            )
        )
        return self._proxy_context().value(handle.handle, handle.type)

    async def get_flake(self, ref: str | dict[str, Any], *, timeout: float | None = None) -> FlakeRef:
        from nanopynix_proto.nix.eval import GetFlakeRequest

        ref_str = ref if isinstance(ref, str) else str(ref)
        return await self._ensure_proxy().get_flake(GetFlakeRequest(ref=ref_str, store_handle=self._store_handle))
