"""Eval session — exclusive worker lock + ValueProxy for eval over RPC."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanopynix.models import Capture

if TYPE_CHECKING:
    from nanopynix._pool import ReservedWorker, _WorkerManager, _WorkerRef
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

    __slots__ = ("_worker", "_handle", "_type", "_timeout", "_active", "_released")

    def __init__(
        self,
        worker: _WorkerRef,
        handle: int,
        typ: str,
        timeout: float | None = None,
        _active: list[bool] | None = None,
    ) -> None:
        self._worker = worker
        self._handle = handle
        self._type = typ
        self._timeout = timeout
        self._active = _active
        self._released = False

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
        return self._handle

    @property
    def type_name(self) -> str:
        return self._type

    # ── force ──────────────────────────────────────────────────────

    async def force(self, *, timeout: float | None = None) -> ValueAttrs | ValueList | int | str | bool:
        """Evaluate to WHNF.  Compound types return lazy wrappers."""
        self._check_active()
        if self._type == "attrs":
            keys = await self.attr_names(timeout=timeout)
            return ValueAttrs(self._worker, self._handle, keys, timeout=self._timeout, _active=self._active)
        if self._type == "list":
            length = await self.list_length(timeout=timeout)
            return ValueList(self._worker, self._handle, length, timeout=self._timeout, _active=self._active)
        # scalar — delegate to worker
        return await self._worker.send_recv(
            "eval", "force", [self._handle], timeout=self._resolve_timeout(timeout),
        )

    async def force_deep(self, *, timeout: float | None = None):
        """Recursive force — returns plain Python dict/list/scalar."""
        self._check_active()
        return await self._worker.send_recv(
            "eval", "force_deep", [self._handle], timeout=self._resolve_timeout(timeout),
        )

    # ── navigation ─────────────────────────────────────────────────

    async def attr(self, name: str, *, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        result = await self._worker.send_recv(
            "eval", "attr", [self._handle, name], timeout=self._resolve_timeout(timeout),
        )
        return ValueProxy(self._worker, result["handle"], result["type"], timeout=self._timeout, _active=self._active)

    async def list_get(self, idx: int, *, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        result = await self._worker.send_recv(
            "eval", "list_get", [self._handle, idx], timeout=self._resolve_timeout(timeout),
        )
        return ValueProxy(self._worker, result["handle"], result["type"], timeout=self._timeout, _active=self._active)

    async def list_length(self, *, timeout: float | None = None) -> int:
        self._check_active()
        return await self._worker.send_recv(
            "eval", "list_length", [self._handle], timeout=self._resolve_timeout(timeout),
        )

    async def attr_names(self, *, timeout: float | None = None) -> list[str]:
        self._check_active()
        return await self._worker.send_recv(
            "eval", "attr_names", [self._handle], timeout=self._resolve_timeout(timeout),
        )

    async def has_attr(self, name: str, *, timeout: float | None = None) -> bool:
        self._check_active()
        return await self._worker.send_recv(
            "eval", "has_attr", [self._handle, name], timeout=self._resolve_timeout(timeout),
        )

    async def call(self, *args, timeout: float | None = None) -> ValueProxy:
        self._check_active()
        result = await self._worker.send_recv(
            "eval", "call", [self._handle, list(args)], timeout=self._resolve_timeout(timeout),
        )
        return ValueProxy(self._worker, result["handle"], result["type"], timeout=self._timeout, _active=self._active)

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
        self._check_active()
        await self._worker.send_recv(
            "eval", "release", [self._handle], timeout=self._resolve_timeout(timeout),
        )
        self._released = True

    def _resolve_timeout(self, override: float | None) -> float | None:
        return override if override is not None else self._timeout


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
        """Return a lazy proxy — no RPC yet."""
        self._check_active()
        return ValueProxy(self._worker, self._handle, "thunk", timeout=self._timeout, _active=self._active)

    async def force(self, name: str, *, timeout=None):
        """Force a single attribute and return its value."""
        self._check_active()
        result = await self._worker.send_recv(
            "eval", "attr", [self._handle, name],
            timeout=timeout if timeout is not None else self._timeout,
        )
        proxy = ValueProxy(self._worker, result["handle"], result["type"],
                           timeout=self._timeout, _active=self._active)
        return await proxy.force()

    async def release(self):
        self._check_active()
        await self._worker.send_recv(
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
        return ValueProxy(self._worker, self._handle, "thunk", timeout=self._timeout, _active=self._active)

    async def force(self, idx: int, *, timeout=None):
        """Force a single element and return its value."""
        self._check_active()
        result = await self._worker.send_recv(
            "eval", "list_get", [self._handle, idx],
            timeout=timeout if timeout is not None else self._timeout,
        )
        proxy = ValueProxy(self._worker, result["handle"], result["type"],
                           timeout=self._timeout, _active=self._active)
        return await proxy.force()

    async def release(self):
        self._check_active()
        await self._worker.send_recv(
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
        self._rw = await self._manager.reserve()
        self._active[0] = True
        return self

    async def __aexit__(self, *args: object) -> None:
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

    async def file(self, path: str, *, timeout: float | None = None,
                   capture: bool = False) -> ValueProxy | Capture[ValueProxy]:
        self._check_rw()
        result = await self._rw.send_recv("eval", "eval_file", [path], timeout=self._resolve_timeout(timeout))
        vp = ValueProxy(self._rw.worker, result["handle"], result["type"], timeout=self._timeout, _active=self._active)
        return Capture(vp) if capture else vp

    async def string(self, expr: str, path: str = "<string>", *, timeout: float | None = None,
                     capture: bool = False) -> ValueProxy | Capture[ValueProxy]:
        self._check_rw()
        result = await self._rw.send_recv("eval", "eval_string", [expr, path], timeout=self._resolve_timeout(timeout))
        vp = ValueProxy(self._rw.worker, result["handle"], result["type"], timeout=self._timeout, _active=self._active)
        return Capture(vp) if capture else vp

    # backward compat
    async def eval_file(self, path: str, *, timeout: float | None = None) -> ValueProxy:
        return await self.file(path, timeout=timeout)
    async def eval_string(self, expr: str, path: str = "<string>", *, timeout: float | None = None) -> ValueProxy:
        return await self.string(expr, path=path, timeout=timeout)

    def _resolve_timeout(self, override: float | None) -> float | None:
        return override if override is not None else self._timeout
