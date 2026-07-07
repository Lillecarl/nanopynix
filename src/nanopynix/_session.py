"""Eval session — exclusive worker lock + ValueProxy for eval over RPC."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from nanopynix._rpc import reserved_call
from nanopynix.models import Capture

if TYPE_CHECKING:
    from nanopynix._pool import ReservedWorker, _WorkerManager
    from nanopynix.store import StoreHandle


# ════════════════════════════════════════════════════════════════════
# ValueProxy — thin RPC client for exported eval handles
# ════════════════════════════════════════════════════════════════════

class ValueProxy:
    """Proxy for a Nix Value exported on the remote worker.

    Lifetime is tied to the ``EvalSession`` that created it — all RPC
    methods raise ``RuntimeError`` after the session exits.

    Supports ``async with`` for early release.
    """

    __slots__ = (
        "_worker",
        "_handle",
        "_type",
        "_timeout",
        "_active",
        "_released",
        "_parent_proxy",
        "_parent_handle",
        "_selector",
    )

    def __init__(
        self,
        worker: _WorkerManager,
        handle: int | None,
        typ: str | None,
        timeout: float | None = None,
        _active: list[bool] | None = None,
        _parent_proxy: ValueProxy | None = None,
        _parent_handle: int | None = None,
        _selector: str | int | None = None,
    ) -> None:
        self._worker = worker
        self._handle = handle
        self._type = typ
        self._timeout = timeout
        self._active = _active
        self._released = False
        self._parent_proxy = _parent_proxy
        self._parent_handle = _parent_handle
        self._selector = _selector

    @classmethod
    def child(
        cls,
        worker: _WorkerManager,
        parent: ValueProxy | int,
        selector: str | int,
        timeout: float | None = None,
        _active: list[bool] | None = None,
    ) -> ValueProxy:
        parent_proxy = parent if isinstance(parent, ValueProxy) else None
        parent_handle = parent if isinstance(parent, int) else None
        return cls(
            worker,
            None,
            None,
            timeout=timeout,
            _active=_active,
            _parent_proxy=parent_proxy,
            _parent_handle=parent_handle,
            _selector=selector,
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
        if self._handle is None:
            raise RuntimeError("ValueProxy has not been resolved yet")
        return self._handle

    @property
    def type_name(self) -> str:
        return self._type or "unknown"

    async def _ensure_resolved(self, *, timeout: float | None = None) -> None:
        self._check_active()
        if self._handle is not None:
            return
        if self._parent_handle is None and self._parent_proxy is not None:
            await self._parent_proxy._ensure_resolved(timeout=timeout)
            self._parent_handle = self._parent_proxy.handle
        if self._parent_handle is None or self._selector is None:
            raise RuntimeError("ValueProxy has no handle or selector")
        t = self._resolve_timeout(timeout)
        if isinstance(self._selector, str):
            result = await self._worker._send_recv(
                "eval", "attr", [self._parent_handle, self._selector], timeout=t,
            )
        else:
            result = await self._worker._send_recv(
                "eval", "list_get", [self._parent_handle, self._selector], timeout=t,
            )
        result = cast(dict[str, Any], result)
        self._handle = result["handle"]
        self._type = result["type"]
        self._parent_proxy = None
        self._parent_handle = None
        self._selector = None

    # ── force ──────────────────────────────────────────────────────

    async def force(self, *, timeout: float | None = None) -> ValueAttrs | ValueList | int | str | bool:
        """Evaluate to WHNF.  Compound types return lazy wrappers."""
        await self._ensure_resolved(timeout=timeout)
        if self._type in (None, "thunk", "unknown"):
            self._type = await self.type(timeout=timeout)
        if self._type == "attrs":
            keys = await self.attr_names(timeout=timeout)
            return ValueAttrs(self._worker, self.handle, keys, timeout=self._timeout, _active=self._active)
        if self._type == "list":
            length = await self.list_length(timeout=timeout)
            return ValueList(self._worker, self.handle, length, timeout=self._timeout, _active=self._active)
        # scalar — delegate to worker
        return cast(ValueAttrs | ValueList | int | str | bool, await self._worker._send_recv(
            "eval", "force", [self.handle], timeout=self._resolve_timeout(timeout),
        ))

    async def force_deep(self, *, timeout: float | None = None):
        """Recursive force — returns plain Python dict/list/scalar."""
        await self._ensure_resolved(timeout=timeout)
        return await self._worker._send_recv(
            "eval", "force_deep", [self.handle], timeout=self._resolve_timeout(timeout),
        )

    # ── navigation ─────────────────────────────────────────────────

    def attr(self, name: str, *, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        parent: ValueProxy | int = self if self._handle is None else self.handle
        return ValueProxy.child(self._worker, parent, name, timeout=self._resolve_timeout(timeout), _active=self._active)

    def list_get(self, idx: int, *, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        parent: ValueProxy | int = self if self._handle is None else self.handle
        return ValueProxy.child(self._worker, parent, idx, timeout=self._resolve_timeout(timeout), _active=self._active)

    async def list_length(self, *, timeout: float | None = None) -> int:
        await self._ensure_resolved(timeout=timeout)
        return cast(int, await self._worker._send_recv(
            "eval", "list_length", [self.handle], timeout=self._resolve_timeout(timeout),
        ))

    async def attr_names(self, *, timeout: float | None = None) -> list[str]:
        await self._ensure_resolved(timeout=timeout)
        return cast(list[str], await self._worker._send_recv(
            "eval", "attr_names", [self.handle], timeout=self._resolve_timeout(timeout),
        ))

    async def has_attr(self, name: str, *, timeout: float | None = None) -> bool:
        await self._ensure_resolved(timeout=timeout)
        return cast(bool, await self._worker._send_recv(
            "eval", "has_attr", [self.handle, name], timeout=self._resolve_timeout(timeout),
        ))

    async def call(self, *args, timeout: float | None = None) -> ValueProxy:
        await self._ensure_resolved(timeout=timeout)
        result = cast(dict[str, Any], await self._worker._send_recv(
            "eval", "call", [self.handle, list(args)], timeout=self._resolve_timeout(timeout),
        ))
        return ValueProxy(self._worker, result["handle"], result["type"], timeout=self._timeout, _active=self._active)

    async def type(self, *, timeout: float | None = None) -> str:
        await self._ensure_resolved(timeout=timeout)
        self._type = cast(str, await self._worker._send_recv(
            "eval", "type_name", [self.handle], timeout=self._resolve_timeout(timeout),
        ))
        return self._type

    # ── type helpers ───────────────────────────────────────────────

    def is_int(self) -> bool:      return self._type == "int"
    def is_string(self) -> bool:   return self._type == "string"
    def is_bool(self) -> bool:     return self._type == "bool"
    def is_attrs(self) -> bool:    return self._type == "attrs"
    def is_list(self) -> bool:     return self._type == "list"
    def is_null(self) -> bool:     return self._type == "null"
    def is_function(self) -> bool: return self._type == "function"

    # ── release ────────────────────────────────────────────────────

    async def release(self, *, timeout: float | None = None) -> None:
        if self._handle is None:
            self._released = True
            return
        self._check_active()
        await self._worker._send_recv(
            "eval", "release", [self.handle], timeout=self._resolve_timeout(timeout),
        )
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

    __slots__ = ("_worker", "_handle", "_keys", "_timeout", "_active", "_released")

    def __init__(self, worker, handle, keys, timeout=None, _active=None):
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

    def _check_active(self):
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

    async def force(self, name: str, *, timeout=None):
        """Force a single attribute and return its value."""
        self._check_active()
        result = await self._worker._send_recv(
            "eval", "attr", [self._handle, name],
            timeout=timeout if timeout is not None else self._timeout,
        )
        proxy = ValueProxy(self._worker, result["handle"], result["type"],
                           timeout=self._timeout, _active=self._active)
        return await proxy.force()

    async def release(self):
        self._check_active()
        await self._worker._send_recv(
            "eval", "release", [self._handle],
            timeout=self._timeout,
        )
        self._released = True


# ════════════════════════════════════════════════════════════════════
# ValueList — lazy list (length accessible, elements lazy)
# ════════════════════════════════════════════════════════════════════

class ValueList:
    """List forced to WHNF — length is available, elements are still lazy.

    ``__getitem__`` returns a ``ValueProxy``.  ``__aenter__``/``__aexit__``
    support early release of the underlying handle.
    """

    __slots__ = ("_worker", "_handle", "_length", "_timeout", "_active", "_released")

    def __init__(self, worker, handle, length, timeout=None, _active=None):
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

    def _check_active(self):
        if self._active is not None and not self._active[0]:
            raise RuntimeError("ValueList is invalid — the EvalSession has been closed")
        if self._released:
            raise RuntimeError("ValueList has been released")

    def __len__(self) -> int:
        return self._length

    def __getitem__(self, idx: int) -> ValueProxy:
        self._check_active()
        return ValueProxy.child(self._worker, self._handle, idx, timeout=self._timeout, _active=self._active)

    async def force(self, idx: int, *, timeout=None):
        """Force a single element and return its value."""
        self._check_active()
        result = await self._worker._send_recv(
            "eval", "list_get", [self._handle, idx],
            timeout=timeout if timeout is not None else self._timeout,
        )
        proxy = ValueProxy(self._worker, result["handle"], result["type"],
                           timeout=self._timeout, _active=self._active)
        return await proxy.force()

    async def release(self):
        self._check_active()
        await self._worker._send_recv(
            "eval", "release", [self._handle],
            timeout=self._timeout,
        )
        self._released = True


# ════════════════════════════════════════════════════════════════════
# EvalSession
# ════════════════════════════════════════════════════════════════════

class EvalSession:
    """Holds the worker exclusively for the duration of an eval session.

    All ``ValueProxy`` instances created through this session become
    invalid after ``__aexit__`` — their RPC methods raise ``RuntimeError``.
    """

    __slots__ = ("_manager", "_rw", "_timeout", "_active")

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
        self._rw = await self._manager.reserve()
        self._active[0] = True

    async def close(self) -> None:
        self._active[0] = False
        if self._rw is not None:
            try:
                await self._rw.send_recv("eval", "release_all", [], timeout=self._timeout)
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

    async def file(self, path: str, *, timeout: float | None = None,
                   capture: bool = False) -> ValueProxy | Capture[ValueProxy]:
        self._check_rw()
        rw = self._reserved_worker()

        def adapter(value: Any) -> ValueProxy:
            value = cast(dict[str, Any], value)
            return ValueProxy(rw._manager, value["handle"], value["type"], timeout=self._timeout, _active=self._active)

        return await reserved_call(
            rw,
            "eval",
            "eval_file",
            [path],
            adapter,
            timeout=self._resolve_timeout(timeout),
            capture=capture,
        )

    async def string(self, expr: str, path: str = "<string>", *, timeout: float | None = None,
                     capture: bool = False) -> ValueProxy | Capture[ValueProxy]:
        self._check_rw()
        rw = self._reserved_worker()

        def adapter(value: Any) -> ValueProxy:
            value = cast(dict[str, Any], value)
            return ValueProxy(rw._manager, value["handle"], value["type"], timeout=self._timeout, _active=self._active)

        return await reserved_call(
            rw,
            "eval",
            "eval_string",
            [expr, path],
            adapter,
            timeout=self._resolve_timeout(timeout),
            capture=capture,
        )

    async def lock_flake(self, ref: str | dict, *, timeout: float | None = None,
                         capture: bool = False) -> Capture | object:
        self._check_rw()
        rw = self._reserved_worker()
        from nanopynix.models import LockedFlake
        return await reserved_call(
            rw,
            "eval",
            "lock_flake",
            [ref],
            LockedFlake.model_validate,
            timeout=self._resolve_timeout(timeout),
            capture=capture,
        )

    async def get_flake(self, ref: str | dict, *, timeout: float | None = None,
                        capture: bool = False) -> Capture | object:
        self._check_rw()
        rw = self._reserved_worker()
        from nanopynix.models import FlakeRef
        return await reserved_call(
            rw,
            "eval",
            "get_flake",
            [ref],
            FlakeRef.model_validate,
            timeout=self._resolve_timeout(timeout),
            capture=capture,
        )

    # backward compat
    async def eval_file(self, path: str, *, timeout: float | None = None) -> ValueProxy:
        return cast(ValueProxy, await self.file(path, timeout=timeout))
    async def eval_string(self, expr: str, path: str = "<string>", *, timeout: float | None = None) -> ValueProxy:
        return cast(ValueProxy, await self.string(expr, path=path, timeout=timeout))

    def _resolve_timeout(self, override: float | None) -> float | None:
        return override if override is not None else self._timeout
