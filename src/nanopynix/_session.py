"""Eval session — exclusive worker lock + ValueProxy for eval over RPC."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, overload

from nanopynix import _protocol as rpc
from nanopynix.models import FlakeRef, JsonScalar, JsonValue, LockedFlake, NixType
from nanopynix.models import DeepAttrs, DeepList, DeepScalar, DeepValueWire, JsonCallArg, RemoteCallArg, RemoteValueRef

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
    type_name: NixType | None


@dataclass(frozen=True)
class _LazyValue:
    parent: ValueProxy | int
    selector: str | int
    type_name: NixType | None = None


type _ValueState = _ResolvedValue | _LazyValue
type _ActiveFlag = list[bool]
type NixArg = ValueProxy | JsonValue
type NixValue = ValueProxy | ValueAttrs | ValueList | JsonValue
type NixDeepValue = ValueProxy | JsonScalar | list[NixDeepValue] | dict[str, NixDeepValue]


def _parse_nix_type(value: NixType | str | None) -> NixType | None:
    if value is None:
        return None
    return value if isinstance(value, NixType) else NixType(value)


class ValueProxy:
    """Proxy for a Nix Value exported on the remote worker.

    Lifetime is tied to the ``EvalSession`` that created it — all RPC
    methods raise ``RuntimeError`` after the session exits.

    Supports ``async with`` for early release.
    """

    __slots__ = (
        "_active",
        "_released",
        "_state",
        "_timeout",
        "_worker",
    )

    def __init__(
        self,
        worker: _EvalWorker,
        handle: int | None,
        typ: NixType | str | None,
        timeout: float | None = None,
        _active: _ActiveFlag | None = None,
        _state: _ValueState | None = None,
    ) -> None:
        self._worker = worker
        if _state is not None:
            self._state = _state
        elif handle is not None:
            self._state = _ResolvedValue(handle=handle, type_name=_parse_nix_type(typ))
        else:
            raise ValueError("ValueProxy requires either a handle or explicit state")
        self._timeout = timeout
        self._active = _active
        self._released = False

    @classmethod
    def child(
        cls,
        worker: _EvalWorker,
        parent: ValueProxy | int,
        selector: str | int,
        timeout: float | None = None,
        _active: _ActiveFlag | None = None,
    ) -> ValueProxy:
        return cls(
            worker,
            None,
            None,
            timeout=timeout,
            _active=_active,
            _state=_LazyValue(parent=parent, selector=selector),
        )

    async def __aenter__(self) -> ValueProxy:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

    def _check_active(self) -> None:
        if self._active is not None and not self._active[0]:
            raise RuntimeError("ValueProxy is invalid — the EvalSession has been closed")
        if self._released:
            raise RuntimeError("ValueProxy has been released")

    @property
    def handle(self) -> int:
        if not isinstance(self._state, _ResolvedValue):
            raise RuntimeError("ValueProxy has not been resolved yet")
        return self._state.handle

    @property
    def type_name(self) -> str:
        return self.nix_type.value

    @property
    def nix_type(self) -> NixType:
        return self._state.type_name or NixType.UNKNOWN

    async def _ensure_resolved(self, *, timeout: float | None = None) -> None:
        self._check_active()
        if isinstance(self._state, _ResolvedValue):
            return
        lazy = self._state
        if isinstance(lazy.parent, ValueProxy):
            await lazy.parent._ensure_resolved(timeout=timeout)
            parent_handle = lazy.parent.handle
        else:
            parent_handle = lazy.parent
        t = self._resolve_timeout(timeout)
        if isinstance(lazy.selector, str):
            handle = await self._worker.request(rpc.Attr(handle=parent_handle, name=lazy.selector), timeout=t)
        else:
            handle = await self._worker.request(rpc.ListGet(handle=parent_handle, index=lazy.selector), timeout=t)
        self._state = _ResolvedValue(handle=handle.handle, type_name=handle.type)

    def _decode_remote_ref(self, ref: RemoteValueRef) -> ValueProxy:
        handle = ref.value
        return ValueProxy(self._worker, handle.handle, handle.type, timeout=self._timeout, _active=self._active)

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

    async def _encode_call_arg(self, value: NixArg, *, timeout: float | None) -> JsonCallArg | RemoteCallArg:
        if isinstance(value, ValueProxy):
            if value._worker is not self._worker:
                raise ValueError("cannot pass a ValueProxy from another EvalSession")
            await value._ensure_resolved(timeout=timeout)
            return RemoteCallArg(handle=value.handle)
        return JsonCallArg(value=value)

    # ── force ──────────────────────────────────────────────────────

    async def force(self, *, timeout: float | None = None) -> NixValue:
        """Evaluate to WHNF.  Compound types return lazy wrappers."""
        await self._ensure_resolved(timeout=timeout)
        typ = self._state.type_name
        if typ in (None, NixType.THUNK, NixType.UNKNOWN):
            typ = await self.get_type(timeout=timeout)
        if typ == NixType.ATTRS:
            keys = await self.attr_names(timeout=timeout)
            return ValueAttrs(
                self._worker,
                self.handle,
                keys,
                timeout=self._timeout,
                _active=self._active,
            )
        if typ == NixType.LIST:
            length = await self.list_length(timeout=timeout)
            return ValueList(
                self._worker,
                self.handle,
                length,
                timeout=self._timeout,
                _active=self._active,
            )
        if typ == NixType.FUNCTION:
            return self
        # scalar — delegate to worker
        result = await self._worker.request(rpc.Force(handle=self.handle), timeout=self._resolve_timeout(timeout))
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
        actual = await self.get_type(timeout=timeout)
        if actual != typ:
            raise TypeError(f"Nix value is {actual.value}, expected {typ.value}")
        return await self.force(timeout=timeout)

    async def force_deep(self, *, timeout: float | None = None) -> NixDeepValue:
        """Recursive Nix force. Functions remain remote callable ValueProxy objects."""
        await self._ensure_resolved(timeout=timeout)
        result = await self._worker.request(rpc.ForceDeep(handle=self.handle), timeout=self._resolve_timeout(timeout))
        return self._decode_deep_value(result)

    # ── navigation ─────────────────────────────────────────────────

    def attr(self, name: str, *, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        parent: ValueProxy | int = self if isinstance(self._state, _LazyValue) else self.handle
        return ValueProxy.child(
            self._worker, parent, name, timeout=self._resolve_timeout(timeout), _active=self._active
        )

    def list_get(self, idx: int, *, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        parent: ValueProxy | int = self if isinstance(self._state, _LazyValue) else self.handle
        return ValueProxy.child(self._worker, parent, idx, timeout=self._resolve_timeout(timeout), _active=self._active)

    async def list_length(self, *, timeout: float | None = None) -> int:
        await self._ensure_resolved(timeout=timeout)
        return await self._worker.request(rpc.ListLength(handle=self.handle), timeout=self._resolve_timeout(timeout))

    async def attr_names(self, *, timeout: float | None = None) -> list[str]:
        await self._ensure_resolved(timeout=timeout)
        return await self._worker.request(rpc.AttrNames(handle=self.handle), timeout=self._resolve_timeout(timeout))

    async def has_attr(self, name: str, *, timeout: float | None = None) -> bool:
        await self._ensure_resolved(timeout=timeout)
        return await self._worker.request(
            rpc.HasAttr(handle=self.handle, name=name),
            timeout=self._resolve_timeout(timeout),
        )

    async def call(self, *args: NixArg, timeout: float | None = None) -> ValueProxy:
        await self._ensure_resolved(timeout=timeout)
        actual = await self.get_type(timeout=timeout)
        if actual != NixType.FUNCTION:
            raise TypeError(f"Nix value is {actual.value}, expected function")
        t = self._resolve_timeout(timeout)
        call_args = [await self._encode_call_arg(arg, timeout=timeout) for arg in args]
        result = await self._worker.request(
            rpc.Call(handle=self.handle, args=call_args),
            timeout=t,
        )
        return ValueProxy(self._worker, result.handle, result.type, timeout=self._timeout, _active=self._active)

    async def __call__(self, *args: NixArg, timeout: float | None = None) -> ValueProxy:
        return await self.call(*args, timeout=timeout)

    async def get_type(self, *, timeout: float | None = None) -> NixType:
        await self._ensure_resolved(timeout=timeout)
        type_name = await self._worker.request(rpc.TypeName(handle=self.handle), timeout=self._resolve_timeout(timeout))
        self._state = _ResolvedValue(handle=self.handle, type_name=type_name)
        return type_name

    # ── release ────────────────────────────────────────────────────

    async def release(self, *, timeout: float | None = None) -> None:
        if not isinstance(self._state, _ResolvedValue):
            self._released = True
            return
        self._check_active()
        await self._worker.request(rpc.Release(handle=self.handle), timeout=self._resolve_timeout(timeout))
        self._released = True

    def _resolve_timeout(self, override: float | None) -> float | None:
        return override if override is not None else self._timeout


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

    __slots__ = ("_active", "_handle", "_keys", "_released", "_timeout", "_worker")

    def __init__(
        self,
        worker: _EvalWorker,
        handle: int,
        keys: Sequence[str],
        timeout: float | None = None,
        _active: _ActiveFlag | None = None,
    ) -> None:
        self._worker = worker
        self._handle = handle
        self._keys = keys
        self._timeout = timeout
        self._active = _active
        self._released = False

    async def __aenter__(self) -> ValueAttrs:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

    def _check_active(self) -> None:
        if self._active is not None and not self._active[0]:
            raise RuntimeError("ValueAttrs is invalid — the EvalSession has been closed")
        if self._released:
            raise RuntimeError("ValueAttrs has been released")

    def keys(self) -> list[str]:
        return list(self._keys)

    def __getitem__(self, name: str) -> ValueProxy:
        """Return a lazy child proxy — the RPC fires on ``await .force()``."""
        self._check_active()
        return ValueProxy.child(self._worker, self._handle, name, timeout=self._timeout, _active=self._active)

    async def force(self, name: str, *, timeout: float | None = None) -> NixValue:
        """Force a single attribute and return its value."""
        self._check_active()
        result = await self._worker.request(
            rpc.Attr(handle=self._handle, name=name),
            timeout=timeout if timeout is not None else self._timeout,
        )
        proxy = ValueProxy(
            self._worker,
            result.handle,
            result.type,
            timeout=self._timeout,
            _active=self._active,
        )
        return await proxy.force()

    async def release(self) -> None:
        self._check_active()
        await self._worker.request(rpc.Release(handle=self._handle), timeout=self._timeout)
        self._released = True


# ════════════════════════════════════════════════════════════════════
# ValueList — lazy list (length accessible, elements lazy)
# ════════════════════════════════════════════════════════════════════


class ValueList:
    """List forced to WHNF — length is available, elements are still lazy.

    ``__getitem__`` returns a ``ValueProxy``.  ``__aenter__``/``__aexit__``
    support early release of the underlying handle.
    """

    __slots__ = ("_active", "_handle", "_length", "_released", "_timeout", "_worker")

    def __init__(
        self,
        worker: _EvalWorker,
        handle: int,
        length: int,
        timeout: float | None = None,
        _active: _ActiveFlag | None = None,
    ) -> None:
        self._worker = worker
        self._handle = handle
        self._length = length
        self._timeout = timeout
        self._active = _active
        self._released = False

    async def __aenter__(self) -> ValueList:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

    def _check_active(self) -> None:
        if self._active is not None and not self._active[0]:
            raise RuntimeError("ValueList is invalid — the EvalSession has been closed")
        if self._released:
            raise RuntimeError("ValueList has been released")

    def __len__(self) -> int:
        return self._length

    def __getitem__(self, idx: int) -> ValueProxy:
        self._check_active()
        return ValueProxy.child(self._worker, self._handle, idx, timeout=self._timeout, _active=self._active)

    async def force(self, idx: int, *, timeout: float | None = None) -> NixValue:
        """Force a single element and return its value."""
        self._check_active()
        result = await self._worker.request(
            rpc.ListGet(handle=self._handle, index=idx),
            timeout=timeout if timeout is not None else self._timeout,
        )
        proxy = ValueProxy(
            self._worker,
            result.handle,
            result.type,
            timeout=self._timeout,
            _active=self._active,
        )
        return await proxy.force()

    async def release(self) -> None:
        self._check_active()
        await self._worker.request(rpc.Release(handle=self._handle), timeout=self._timeout)
        self._released = True


# ════════════════════════════════════════════════════════════════════
# EvalSession
# ════════════════════════════════════════════════════════════════════


class EvalSession:
    """Holds the worker exclusively for the duration of an eval session.

    All ``ValueProxy`` instances created through this session become
    invalid after ``__aexit__`` — their RPC methods raise ``RuntimeError``.
    """

    __slots__ = ("_active", "_manager", "_rw", "_timeout")

    def __init__(self, manager: _WorkerManager, timeout: float | None = None) -> None:
        self._manager = manager
        self._rw: ReservedWorker | None = None
        self._timeout = timeout
        self._active: list[bool] = [False]

    async def __aenter__(self) -> EvalSession:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._rw is not None:
            return
        self._rw = await self._manager.reserve(timeout=self._timeout)
        self._active[0] = True

    async def close(self) -> None:
        self._active[0] = False
        if self._rw is not None:
            try:
                await self._rw.request(rpc.ReleaseAll(), timeout=self._timeout)
            finally:
                await self._rw.release()
                self._rw = None

    def _check_rw(self) -> None:
        if self._rw is None:
            raise RuntimeError("EvalSession not entered — use 'async with session.eval() as eval_:'")

    def _reserved_worker(self) -> ReservedWorker:
        self._check_rw()
        assert self._rw is not None
        return self._rw

    async def file(self, path: str, *, timeout: float | None = None) -> ValueProxy:
        self._check_rw()
        rw = self._reserved_worker()

        handle = await rw.request(rpc.EvalFile(path=path), timeout=self._resolve_timeout(timeout))
        return ValueProxy(rw, handle.handle, handle.type, timeout=self._timeout, _active=self._active)

    async def string(
        self, expr: str, path: str = "<string>", *, timeout: float | None = None
    ) -> ValueProxy:
        self._check_rw()
        rw = self._reserved_worker()

        handle = await rw.request(
            rpc.EvalString(expr=expr, source_name=path),
            timeout=self._resolve_timeout(timeout),
        )
        return ValueProxy(rw, handle.handle, handle.type, timeout=self._timeout, _active=self._active)

    async def lock_flake(self, ref: str | dict[str, Any], *, timeout: float | None = None) -> LockedFlake:
        self._check_rw()
        rw = self._reserved_worker()
        return await rw.request(rpc.LockFlake(ref=ref), timeout=self._resolve_timeout(timeout))

    async def get_flake(self, ref: str | dict[str, Any], *, timeout: float | None = None) -> FlakeRef:
        self._check_rw()
        rw = self._reserved_worker()
        return await rw.request(rpc.GetFlake(ref=ref), timeout=self._resolve_timeout(timeout))

    def _resolve_timeout(self, override: float | None) -> float | None:
        return override if override is not None else self._timeout
