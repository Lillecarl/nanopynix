# ruff: noqa: ASYNC109
"""Eval session — exclusive worker lock + ValueProxy for eval over RPC."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING, Any, Literal, overload

from nanopynix import _protocol as rpc
from nanopynix.exceptions import (
    EvalSessionClosedError,
    ForeignValueError,
    NixCoercionError,
    UnresolvedValueError,
    ValueReleasedError,
    WrongNixTypeError,
)
from nanopynix.models import (
    AttrsCallArg,
    CallArgWire,
    DeepAttrs,
    DeepList,
    DeepScalar,
    DeepValueWire,
    FlakeRef,
    JsonScalar,
    JsonValue,
    ListCallArg,
    LockedFlake,
    NixType,
    RemoteCallArg,
    RemoteValueRef,
    ScalarCallArg,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nanopynix._pool import ReservedWorker, _WorkerManager

    type _EvalWorker = ReservedWorker | _WorkerManager


# ════════════════════════════════════════════════════════════════════
# ValueProxy — thin RPC client for exported eval handles
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


@dataclass(frozen=True)
class _EvalProxyContext:
    worker: _EvalWorker
    owner: _EvalOwner
    timeout: float | None = None

    def with_timeout(self, timeout: float | None) -> _EvalProxyContext:
        return self if timeout is None else _EvalProxyContext(self.worker, self.owner, timeout)

    def resolve_timeout(self, override: float | None) -> float | None:
        return override if override is not None else self.timeout

    def value(self, handle: int, typ: NixType | str | None) -> ValueProxy:
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


def _parse_nix_type(value: NixType | str | None) -> NixType | None:
    if value is None:
        return None
    return value if isinstance(value, NixType) else NixType(value)


class ValueProxy:
    """Proxy for a Nix Value exported on the remote worker.

    Lifetime is tied to the ``EvalSession`` that created it — all RPC
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
        return self._state.nix_type or NixType.UNKNOWN

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
        t = self._ctx.resolve_timeout(timeout)
        if isinstance(lazy.selector, str):
            handle = await self._ctx.worker.request(rpc.Attr(handle=parent.handle, name=lazy.selector), timeout=t)
        else:
            handle = await self._ctx.worker.request(rpc.ListGet(handle=parent.handle, index=lazy.selector), timeout=t)
        self._state = _ResolvedValue(handle=handle.handle, nix_type=handle.type)

    async def _ensure_type(self, *, timeout: float | None = None) -> NixType:
        await self._ensure_resolved(timeout=timeout)
        cached = self._state.nix_type
        if cached not in (None, NixType.THUNK, NixType.UNKNOWN):
            return cached
        type_name = await self._ctx.worker.request(
            rpc.TypeName(handle=self.handle), timeout=self._ctx.resolve_timeout(timeout)
        )
        self._state = _ResolvedValue(handle=self.handle, nix_type=type_name)
        return type_name

    def _decode_remote_ref(self, ref: RemoteValueRef) -> ValueProxy:
        handle = ref.value
        return self._ctx.value(handle.handle, handle.type)

    def _decode_force_value(self, value: rpc.ForceValueWire) -> JsonValue | ValueProxy:
        if isinstance(value, RemoteValueRef):
            return self._decode_remote_ref(value)
        return value

    def _decode_deep_value(self, value: DeepValueWire) -> NixDeepValue:
        if isinstance(value, RemoteValueRef):
            return self._decode_remote_ref(value)
        if isinstance(value, DeepScalar):
            return value.value
        if isinstance(value, DeepList):
            return [self._decode_deep_value(item) for item in value.items]
        if isinstance(value, DeepAttrs):
            return {key: self._decode_deep_value(item) for key, item in value.attrs.items()}
        raise TypeError(f"unsupported force_deep RPC value: {value!r}")

    async def _encode_call_arg(self, value: NixArg, *, timeout: float | None) -> CallArgWire:
        if isinstance(value, ValueProxy):
            if not self._ctx.owner.owns(value):
                raise ForeignValueError("cannot pass a ValueProxy from another EvalSession")
            await value._ensure_resolved(timeout=timeout)
            return RemoteCallArg(handle=value.handle)
        if isinstance(value, list):
            return ListCallArg(items=[await self._encode_call_arg(item, timeout=timeout) for item in value])
        if isinstance(value, dict):
            attrs = {key: await self._encode_call_arg(item, timeout=timeout) for key, item in value.items()}
            return AttrsCallArg(attrs=attrs)
        return ScalarCallArg(value=value)

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
        result = await self._ctx.worker.request(
            rpc.Force(handle=self.handle), timeout=self._ctx.resolve_timeout(timeout)
        )
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

    async def try_int(self, *, timeout: float | None = None) -> int:
        return await self.force_as(NixType.INT, timeout=timeout)

    async def try_float(self, *, timeout: float | None = None) -> float:
        return await self.force_as(NixType.FLOAT, timeout=timeout)

    async def try_bool(self, *, timeout: float | None = None) -> bool:
        return await self.force_as(NixType.BOOL, timeout=timeout)

    async def try_str(self, *, timeout: float | None = None) -> str:
        return await self.force_as(NixType.STRING, timeout=timeout)

    async def try_path(self, *, timeout: float | None = None) -> str:
        return await self.force_as(NixType.PATH, timeout=timeout)

    async def try_null(self, *, timeout: float | None = None) -> None:
        return await self.force_as(NixType.NULL, timeout=timeout)

    async def try_attrs(self, *, timeout: float | None = None) -> ValueAttrs:
        return await self.force_as(NixType.ATTRS, timeout=timeout)

    async def try_list(self, *, timeout: float | None = None) -> ValueList:
        return await self.force_as(NixType.LIST, timeout=timeout)

    async def try_function(self, *, timeout: float | None = None) -> ValueProxy:
        return await self.force_as(NixType.FUNCTION, timeout=timeout)

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
        raise NixCoercionError(f"cannot coerce Nix {self.nix_type.value} to string")

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
        raise NixCoercionError(f"cannot coerce Nix {self.nix_type.value} to int")

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
            raise NixCoercionError(f"cannot coerce Nix {self.nix_type.value} to float")
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
        raise NixCoercionError(f"cannot coerce Nix {self.nix_type.value} to bool")

    async def force_deep(self, *, timeout: float | None = None) -> NixDeepValue:
        """Recursive Nix force. Functions remain remote callable ValueProxy objects."""
        await self._ensure_resolved(timeout=timeout)
        result = await self._ctx.worker.request(
            rpc.ForceDeep(handle=self.handle), timeout=self._ctx.resolve_timeout(timeout)
        )
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
        return await self._ctx.worker.request(
            rpc.ForceJson(handle=self.handle, copy_to_store=copy_to_store),
            timeout=self._ctx.resolve_timeout(timeout),
        )

    # ── navigation ─────────────────────────────────────────────────

    def attr(self, name: str, *, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        parent: ValueProxy | _ResolvedValue = self if isinstance(self._state, _LazyValue) else self._resolved
        return self._ctx.child(parent, name, timeout=timeout)

    def list_get(self, idx: int, *, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        if idx < 0:
            raise IndexError(f"list index must be non-negative, got {idx}")
        parent: ValueProxy | _ResolvedValue = self if isinstance(self._state, _LazyValue) else self._resolved
        return self._ctx.child(parent, idx, timeout=timeout)

    async def list_length(self, *, timeout: float | None = None) -> int:
        await self._ensure_resolved(timeout=timeout)
        return await self._ctx.worker.request(
            rpc.ListLength(handle=self.handle), timeout=self._ctx.resolve_timeout(timeout)
        )

    async def attr_names(self, *, timeout: float | None = None) -> list[str]:
        await self._ensure_resolved(timeout=timeout)
        return await self._ctx.worker.request(
            rpc.AttrNames(handle=self.handle), timeout=self._ctx.resolve_timeout(timeout)
        )

    async def has_attr(self, name: str, *, timeout: float | None = None) -> bool:
        await self._ensure_resolved(timeout=timeout)
        return await self._ctx.worker.request(
            rpc.HasAttr(handle=self.handle, name=name),
            timeout=self._ctx.resolve_timeout(timeout),
        )

    async def call(self, *args: NixArg, timeout: float | None = None) -> ValueProxy:
        await self._ensure_resolved(timeout=timeout)
        actual = await self._ensure_type(timeout=timeout)
        if actual != NixType.FUNCTION:
            raise WrongNixTypeError(expected=NixType.FUNCTION, actual=actual)
        t = self._ctx.resolve_timeout(timeout)
        call_args = [await self._encode_call_arg(arg, timeout=timeout) for arg in args]
        result = await self._ctx.worker.request(
            rpc.Call(handle=self.handle, args=call_args),
            timeout=t,
        )
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
        await self._ctx.worker.request(rpc.Release(handle=self.handle), timeout=self._ctx.resolve_timeout(timeout))
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
        result = await self._ctx.worker.request(
            rpc.Attr(handle=self._value.handle, name=name),
            timeout=self._ctx.resolve_timeout(timeout),
        )
        proxy = self._ctx.value(result.handle, result.type)
        return await proxy.force()

    async def release(self) -> None:
        self._check_active()
        await self._ctx.worker.request(rpc.Release(handle=self._value.handle), timeout=self._ctx.timeout)
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
        if idx < 0 or idx >= self._length:
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
        result = await self._ctx.worker.request(
            rpc.ListGet(handle=self._value.handle, index=idx),
            timeout=self._ctx.resolve_timeout(timeout),
        )
        proxy = self._ctx.value(result.handle, result.type)
        return await proxy.force()

    async def release(self) -> None:
        self._check_active()
        await self._ctx.worker.request(rpc.Release(handle=self._value.handle), timeout=self._ctx.timeout)
        self._released = True


# ════════════════════════════════════════════════════════════════════
# EvalSession
# ════════════════════════════════════════════════════════════════════


class EvalSession:
    """Holds the worker exclusively for the duration of an eval session.

    All ``ValueProxy`` instances created through this session become
    invalid after ``__aexit__`` — their RPC methods raise ``EvalSessionClosedError``.
    """

    __slots__ = ("_active", "_ctx", "_manager", "_owner", "_rw", "_timeout")

    def __init__(self, manager: _WorkerManager, timeout: float | None = None) -> None:
        self._manager = manager
        self._rw: ReservedWorker | None = None
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
        self._ctx = _EvalProxyContext(self._rw, self._owner, self._timeout)
        self._active[0] = True

    async def close(self) -> None:
        self._active[0] = False
        if self._rw is not None:
            try:
                await self._rw.request(rpc.ReleaseAll(), timeout=self._timeout)
            finally:
                await self._rw.release()
                self._rw = None
                self._ctx = None

    def _check_rw(self) -> None:
        if self._rw is None:
            raise EvalSessionClosedError("EvalSession not entered — use 'async with session.eval() as eval_:'")

    def _reserved_worker(self) -> ReservedWorker:
        rw = self._rw
        if rw is None:
            raise EvalSessionClosedError("EvalSession not entered — use 'async with session.eval() as eval_:'")
        return rw

    def _proxy_context(self) -> _EvalProxyContext:
        ctx = self._ctx
        if ctx is None:
            raise EvalSessionClosedError("EvalSession not entered — use 'async with session.eval() as eval_:'")
        return ctx

    async def file(self, path: str, *, timeout: float | None = None) -> ValueProxy:
        self._check_rw()
        rw = self._reserved_worker()

        handle = await rw.request(rpc.EvalFile(path=path), timeout=self._resolve_timeout(timeout))
        return self._proxy_context().value(handle.handle, handle.type)

    async def string(self, expr: str, path: str = "<string>", *, timeout: float | None = None) -> ValueProxy:
        self._check_rw()
        rw = self._reserved_worker()

        handle = await rw.request(
            rpc.EvalString(expr=expr, source_name=path),
            timeout=self._resolve_timeout(timeout),
        )
        return self._proxy_context().value(handle.handle, handle.type)

    async def lock_flake(
        self,
        ref: str,
        *,
        update_all: bool = False,
        update_inputs: list[str] | None = None,
        write_lock_file: bool = True,
        timeout: float | None = None,
    ) -> LockedFlake:
        """Lock a flake, optionally updating inputs.

        Without update flags, creates missing lock entries only (like
        ``nix flake lock``).  With ``update_all=True``, re-resolves all
        inputs (like ``nix flake update``).  With ``update_inputs=["nixpkgs"]``,
        re-resolves only the specified inputs (like ``nix flake update nixpkgs``).

        Returns a ``LockedFlake`` with a ``handle`` that can be used with
        ``eval_locked_flake()``, ``write_lock_file()``, and
        ``release_locked_flake()``.  When ``write_lock_file=False``, the lock
        is updated in memory only — call ``write_lock_file(handle)`` later to
        persist it to disk.
        """
        self._check_rw()
        rw = self._reserved_worker()
        return await rw.request(
            rpc.LockFlake(
                ref=ref,
                update_all=update_all,
                update_inputs=update_inputs or [],
                write_lock_file=write_lock_file,
            ),
            timeout=self._resolve_timeout(timeout),
        )

    async def eval_locked_flake(self, handle: int, *, timeout: float | None = None) -> ValueProxy:
        """Evaluate a previously locked flake by handle.

        Calls the flake's ``outputs`` function using the in-memory
        ``LockedFlake`` from a prior ``lock_flake()`` call.  This allows
        evaluating with an updated lock that hasn't been written to disk yet.
        """
        self._check_rw()
        rw = self._reserved_worker()
        result = await rw.request(
            rpc.CallLockedFlake(handle=handle),
            timeout=self._resolve_timeout(timeout),
        )
        return self._proxy_context().value(result.handle, result.type)

    async def write_lock_file(self, handle: int, *, timeout: float | None = None) -> None:
        """Write a locked flake's lock file to disk.

        Persists the in-memory lock from a prior ``lock_flake(write_lock_file=False)``
        call.  The lock file is written to the flake's own directory — for a
        temp flake this is the temp directory, never a real path.
        """
        self._check_rw()
        rw = self._reserved_worker()
        await rw.request(
            rpc.WriteLockFile(handle=handle),
            timeout=self._resolve_timeout(timeout),
        )

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
        self._check_rw()
        rw = self._reserved_worker()
        handle = await rw.request(
            rpc.EvalFlake(ref=ref, write_lock_file=write_lock_file),
            timeout=self._resolve_timeout(timeout),
        )
        return self._proxy_context().value(handle.handle, handle.type)

    async def get_flake(self, ref: str | dict[str, Any], *, timeout: float | None = None) -> FlakeRef:
        self._check_rw()
        rw = self._reserved_worker()
        return await rw.request(rpc.GetFlake(ref=ref), timeout=self._resolve_timeout(timeout))

    def _resolve_timeout(self, override: float | None) -> float | None:
        return override if override is not None else self._timeout
