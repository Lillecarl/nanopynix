"""Eval session — exclusive worker lock + ValueProxy for eval over RPC."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanopynix._pool import WorkerPool, _WorkerRef


# ════════════════════════════════════════════════════════════════════
# ValueProxy — thin RPC client for exported eval handles
# ════════════════════════════════════════════════════════════════════

class ValueProxy:
    """Proxy for a Nix Value exported on a remote worker."""

    __slots__ = ("_worker", "_handle", "_type", "_timeout")

    def __init__(
        self,
        worker: _WorkerRef,
        handle: int,
        typ: str,
        timeout: float | None = None,
    ) -> None:
        self._worker = worker
        self._handle = handle
        self._type = typ
        self._timeout = timeout

    @property
    def handle(self) -> int:
        return self._handle

    @property
    def type_name(self) -> str:
        return self._type

    async def force(self, *, timeout: float | None = None):
        return await self._worker.send_recv(
            "eval", "force", [self._handle], timeout=self._resolve_timeout(timeout),
        )

    async def attr(self, name: str, *, timeout: float | None = None) -> ValueProxy:
        result = await self._worker.send_recv(
            "eval", "attr", [self._handle, name], timeout=self._resolve_timeout(timeout),
        )
        return ValueProxy(self._worker, result["handle"], result["type"], timeout=self._timeout)

    async def list_get(self, idx: int, *, timeout: float | None = None) -> ValueProxy:
        result = await self._worker.send_recv(
            "eval", "list_get", [self._handle, idx], timeout=self._resolve_timeout(timeout),
        )
        return ValueProxy(self._worker, result["handle"], result["type"], timeout=self._timeout)

    async def list_length(self, *, timeout: float | None = None) -> int:
        return await self._worker.send_recv(
            "eval", "list_length", [self._handle], timeout=self._resolve_timeout(timeout),
        )

    async def attr_names(self, *, timeout: float | None = None) -> list[str]:
        return await self._worker.send_recv(
            "eval", "attr_names", [self._handle], timeout=self._resolve_timeout(timeout),
        )

    async def has_attr(self, name: str, *, timeout: float | None = None) -> bool:
        return await self._worker.send_recv(
            "eval", "has_attr", [self._handle, name], timeout=self._resolve_timeout(timeout),
        )

    async def release(self, *, timeout: float | None = None) -> None:
        await self._worker.send_recv(
            "eval", "release", [self._handle], timeout=self._resolve_timeout(timeout),
        )

    def _resolve_timeout(self, override: float | None) -> float | None:
        if override is not None:
            return override
        return self._timeout


class EvalSession:
    """Holds a worker exclusively for the duration of an eval session."""

    __slots__ = ("_pool", "_worker", "_timeout")

    def __init__(self, pool: WorkerPool, timeout: float | None = None) -> None:
        self._pool = pool
        self._worker: _WorkerRef | None = None
        self._timeout = timeout

    async def __aenter__(self) -> EvalSession:
        self._worker = await self._pool._acquire()
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._worker is not None:
            await self._worker.send_recv("eval", "release_all", [], timeout=self._timeout)
            await self._pool._release(self._worker)
            self._worker = None

    async def eval_file(self, path: str, *, timeout: float | None = None) -> ValueProxy:
        result = await self._worker.send_recv("eval", "eval_file", [path], timeout=self._resolve_timeout(timeout))
        return ValueProxy(self._worker, result["handle"], result["type"], timeout=self._timeout)

    async def eval_string(self, expr: str, path: str = "<string>", *, timeout: float | None = None) -> ValueProxy:
        result = await self._worker.send_recv("eval", "eval_string", [expr, path], timeout=self._resolve_timeout(timeout))
        return ValueProxy(self._worker, result["handle"], result["type"], timeout=self._timeout)

    def _resolve_timeout(self, override: float | None) -> float | None:
        if override is not None:
            return override
        return self._timeout
