# ruff: noqa: ASYNC109
"""Subprocess worker — Nix execution backend via asyncio subprocess.

A single subprocess runs an independent Nix process with its own Store,
logger, and globals.  Communication is JSON-RPC 2.0 over stdin/stdout
(newline-delimited compact JSON).  Transport is ``StreamReader``/``StreamWriter`` —
the same abstraction asyncssh uses, making remote transport a future option.

Only one call is in-flight at a time — the worker is single-threaded.
``_WorkerManager.call()`` acquires the lock, writes the request, and
waits for the response.  ``_WorkerManager.reserve()`` holds the lock
for the duration of an ``EvalSession``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from nanopynix.exceptions import from_response

if TYPE_CHECKING:
    from nanopynix import _protocol as rpc
    from nanopynix.models import PrimOpSpec

logger = logging.getLogger(__name__)
T = TypeVar("T")

# ────────────────────────────────────────────────────────────────────
_RPC_TIMEOUT = 300.0
_id_counter = itertools.count()

_SEPARATORS = (",", ":")


# ════════════════════════════════════════════════════════════════════
# Exceptions
# ════════════════════════════════════════════════════════════════════


class WorkerDiedError(RuntimeError):
    """Raised when the subprocess worker dies unexpectedly."""


class WorkerBusyError(RuntimeError):
    """Raised when the single worker is already handling another operation."""


@dataclass
class _ActiveCall:
    req_id: int
    future: asyncio.Future


# ════════════════════════════════════════════════════════════════════
# _LogBus + _Subscription
# ════════════════════════════════════════════════════════════════════


class _LogBus:
    """Subscriber list for worker log events.

    Zero subscribers → events are discarded (no buffering, no overhead).
    Callbacks are called synchronously from the read loop — keep them fast.
    """

    def __init__(self) -> None:
        self._subscribers: list = []

    def subscribe(self, callback) -> _Subscription:
        self._subscribers.append(callback)
        return _Subscription(self, callback)

    def _unsubscribe(self, sub: _Subscription) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(sub._callback)

    def emit(self, event: object) -> None:
        if not self._subscribers:
            return
        for cb in self._subscribers:
            try:
                cb(event)
            except Exception:
                logger.exception("worker log subscriber failed")


class _Subscription:
    """Handle returned by ``_LogBus.subscribe()``."""

    __slots__ = ("_bus", "_callback")

    def __init__(self, bus: _LogBus, callback) -> None:
        self._bus = bus
        self._callback = callback

    def unsubscribe(self) -> None:
        self._bus._unsubscribe(self)


# ════════════════════════════════════════════════════════════════════
# ReservedWorker — public token for an exclusive worker lease
# ════════════════════════════════════════════════════════════════════


class ReservedWorker:
    """Exclusive lease on the session worker, obtained via ``_WorkerManager.reserve()``.

    Delegates ``send_recv`` to the underlying manager and
    releases the worker back on ``release()``.
    """

    __slots__ = ("_manager", "_released", "_rpc_lock")

    def __init__(self, manager: _WorkerManager) -> None:
        self._manager = manager
        self._released = False
        self._rpc_lock = asyncio.Lock()

    async def send_recv(
        self,
        module: str,
        fn: str,
        args: list,
        timeout: float | None = None,
    ) -> Any:
        """Send an RPC call on the reserved worker and await the response."""
        if self._released:
            raise RuntimeError("ReservedWorker has been released")
        async with self._rpc_lock:
            return await self._manager._send_recv(module, fn, args, timeout=timeout)

    async def request(self, request: rpc.WorkerRequest[T], timeout: float | None = None) -> T:
        """Send a typed RPC request on the reserved worker."""
        if self._released:
            raise RuntimeError("ReservedWorker has been released")
        async with self._rpc_lock:
            result = await self._manager._send_recv(
                request.namespace,
                request.method,
                request.to_args(),
                timeout=timeout,
            )
        return type(request).parse_response(result)

    async def release(self) -> None:
        """Return the worker to the manager.  Idempotent — safe to call twice."""
        if not self._released:
            self._released = True
            self._manager._release()


# ════════════════════════════════════════════════════════════════════
# _WorkerManager — single-worker lifecycle
# ════════════════════════════════════════════════════════════════════


def _resolve_worker_command() -> list[str]:
    """Resolve the command used to launch the worker subprocess.

    Order of preference:

    1. ``NANOPYNIX_WORKER`` env var — explicit override (shell-split, so it
       may carry flags); useful for pinning the worker in tests.
    2. ``nanopynix-worker`` on ``PATH`` — the wrapped console_script that
       ``buildPythonPackage``/``buildPythonApplication`` installs. This is
       the path that works when nanopynix is consumed as a library
       dependency: the wrapper bakes in a correct ``PYTHONPATH``, so the
       worker can import nanopynix + the compiled bindings. A bare
       ``sys.executable -m`` child would inherit a stripped interpreter
       and fail with ``ModuleNotFoundError``.
    3. ``sys.executable -m nanopynix._worker`` — fallback for raw dev /
       pytest / non-Nix pip installs where nanopynix is already importable
       by the running interpreter.
    """
    override = os.environ.get("NANOPYNIX_WORKER")
    if override:
        return shlex.split(override)
    found = shutil.which("nanopynix-worker")
    if found:
        return [found]
    return [sys.executable, "-m", "nanopynix._worker"]


class _WorkerManager:
    """Manages a single subprocess worker with an independent Nix Store.

    Provides:
    - ``call()`` — acquire→send→release for individual RPC calls (used by Store).
    - ``reserve()`` — exclusive worker lease (used by EvalSession).
    - ``subscribe()`` / ``log_stream()`` — log event access.
    """

    def __init__(
        self,
        *,
        store_uri: str = "auto",
        eval_store_uri: str | None = None,
        nix_conf: str | None = "/etc/nix/nix.conf",
        settings: dict[str, str] | None = None,
        experimental_features: list[str] | None = None,
        primops: list[PrimOpSpec] | None = None,
    ) -> None:
        self._store_uri = store_uri
        self._eval_store_uri = eval_store_uri or store_uri
        self._nix_conf = nix_conf
        self._settings = settings or {}
        self._features = experimental_features or []
        self._primops = primops or []
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._available: asyncio.Lock = asyncio.Lock()
        self._log_bus: _LogBus = _LogBus()
        self._read_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._last_activity: float = time.monotonic()
        self._log_done: asyncio.Event = asyncio.Event()
        self._active_call: _ActiveCall | None = None

    # ── lifecycle ──────────────────────────────────────────────────

    async def open(self) -> None:
        """Spawn the worker subprocess."""
        cmd = _resolve_worker_command()
        logger.debug("nanopynix: spawning worker: %s", cmd)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2**63,  # effectively unlimited — OOM is fine, silent truncation is not
        )
        self._reader = self._proc.stdout
        self._writer = self._proc.stdin

        # Kick off the read loop
        self._read_task = asyncio.ensure_future(self._read_loop())
        # Also read stderr in background for debugging
        self._stderr_task = asyncio.ensure_future(self._read_stderr())

        # Init handshake — send init as first JSON-RPC call
        result = await self._send_recv(
            "init",
            "",
            {
                "store_uri": self._store_uri,
                "eval_store_uri": self._eval_store_uri,
                "nix_conf": self._nix_conf,
                "settings": self._settings,
                "experimental_features": self._features,
                "primops": [spec.model_dump(mode="json") for spec in self._primops],
            },
        )
        if result != "ok":
            raise RuntimeError(f"Worker init failed: {result}")

    async def close(self) -> None:
        """Shut down the worker."""
        if self._writer is not None:
            # Send a JSON-RPC notification that signals shutdown
            try:
                self._write_line({"jsonrpc": "2.0", "method": "shutdown"})
            except Exception:
                logger.debug("failed to send worker shutdown notification", exc_info=True)

        if self._proc is not None:
            stdin = self._proc.stdin
            try:
                if stdin is not None:
                    stdin.close()
            except Exception:
                logger.warning("failed to close worker stdin", exc_info=True)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()

        if self._read_task is not None:
            try:
                await asyncio.wait_for(self._read_task, timeout=2.0)
            except (TimeoutError, Exception):
                self._read_task.cancel()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=2.0)
            except (TimeoutError, Exception):
                self._stderr_task.cancel()

        self._log_done.set()
        self._log_bus.emit(None)

    # ── read loop ──────────────────────────────────────────────────

    async def _read_stderr(self) -> None:
        """Read worker stderr for debugging."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            raise WorkerDiedError("Worker stderr stream is not available")
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            logger.warning("worker stderr: %s", line.decode(errors="replace").rstrip())

    async def _read_loop(self) -> None:
        """Read all messages from worker stdout, route to futures or log bus."""
        if self._reader is None:
            raise WorkerDiedError("Worker stdout stream is not available")
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break  # EOF — worker exited
                self._last_activity = time.monotonic()

                msg = json.loads(line)
                rid = msg.get("id")

                if rid is not None:
                    # Response (has "id") — deliver to the waiting future
                    fut = self._pending.pop(rid, None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                elif "method" in msg and "id" not in msg:
                    # Notification — deliver to log bus
                    event = msg.get("params", msg)
                    self._log_bus.emit(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Worker read loop error: %s", exc)
        finally:
            # Wake all pending futures
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(WorkerDiedError("Worker process died"))
            self._pending.clear()
            self._active_call = None

    # ── send helpers ───────────────────────────────────────────────

    def _write_line(self, msg: dict) -> None:
        """Write a JSON-RPC message as a line to the worker's stdin."""
        if self._writer is None:
            raise WorkerDiedError("Worker stdin stream is not available")
        data = json.dumps(msg, separators=_SEPARATORS).encode() + b"\n"
        self._writer.write(data)

    async def _wait_until_idle(self, timeout: float | None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._active_call is not None:
            active = self._active_call
            if timeout is None:
                raise WorkerBusyError("worker is busy")
            remaining = deadline - time.monotonic() if deadline is not None else timeout
            if remaining <= 0:
                raise WorkerBusyError(f"worker is busy after waiting {timeout}s")
            try:
                await asyncio.wait_for(asyncio.shield(active.future), timeout=remaining)
            except TimeoutError as exc:
                raise WorkerBusyError(f"worker is busy after waiting {timeout}s") from exc
            except Exception:
                # The previous call failed; let its owner clear the active slot.
                pass
            await asyncio.sleep(0)

    # ── RPC ────────────────────────────────────────────────────────

    async def _send_recv(
        self,
        module: str,
        fn: str,
        args: list | dict,
        timeout: float | None = None,
    ) -> Any:
        """Send a call and wait for the matching response.

        The timeout is an *idle* timeout: it resets whenever the worker
        sends any message (log events, etc.), so long-running operations
        don't time out while the worker is active.

        Raises:
            WorkerDiedError: the worker process died.
            TimeoutError: *timeout* seconds elapsed with no activity.
            NixError subclass: the worker returned an error.
        """
        if self._proc is None or self._proc.returncode is not None:
            raise WorkerDiedError("Worker is dead")

        t = _RPC_TIMEOUT if timeout is None else timeout
        await self._wait_until_idle(timeout)
        req_id = next(_id_counter)

        try:
            # Create the response future
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = fut
            self._active_call = _ActiveCall(req_id=req_id, future=fut)

            # Build and write the request
            method = f"{module}.{fn}" if fn else module
            self._write_line(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": args,
                    "id": req_id,
                }
            )
            writer = self._writer
            if writer is None:
                raise WorkerDiedError("Worker stdin stream is not available")
            await writer.drain()

            # Wait for response with idle timeout
            last_seen = self._last_activity = time.monotonic()
            msg: dict[str, Any] | None = None
            while True:
                remaining = t - (time.monotonic() - last_seen)
                if remaining <= 0:
                    self._pending.pop(req_id, None)
                    # Check for a late-arriving response
                    if fut.done():
                        break  # race — it arrived
                    raise TimeoutError(f"Call timed out — no worker activity for {t}s")

                try:
                    async with asyncio.timeout(min(remaining, 1.0)):
                        msg = await fut
                        break
                except TimeoutError:
                    if self._last_activity > last_seen:
                        last_seen = self._last_activity
                    continue

            # Cleanup
            self._pending.pop(req_id, None)
            if msg is None:
                raise WorkerDiedError("No response received")

            # Decode response
            if "result" in msg:
                result = msg["result"]
            elif "error" in msg:
                err = msg["error"]
                data = err.get("data", {})
                raise from_response(
                    error_type=data.get("error_type", "Unknown"),
                    msg=err.get("message", "unknown"),
                    raw=data.get("traceback", ""),
                    info=data.get("info"),
                )
            else:
                raise WorkerDiedError(f"Unexpected response: {msg}")

            return result
        finally:
            if self._active_call is not None and self._active_call.req_id == req_id:
                self._active_call = None

    async def call(
        self,
        module: str,
        fn: str,
        args: list,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send an RPC call on the worker and return the response.

        Acquires the worker lock — only one call in-flight at a time.
        """
        if self._proc is None:
            raise WorkerDiedError("Worker not started")
        if self._available.locked():
            if timeout is None:
                raise WorkerBusyError("worker is busy")
            try:
                await asyncio.wait_for(self._available.acquire(), timeout=timeout)
            except TimeoutError as exc:
                raise WorkerBusyError(f"worker is busy after waiting {timeout}s") from exc
            try:
                return await self._send_recv(module, fn, args, timeout=timeout)
            finally:
                self._available.release()

        async with self._available:
            return await self._send_recv(module, fn, args, timeout=timeout)

    async def request(self, request: rpc.WorkerRequest[T], *, timeout: float | None = None) -> T:
        """Send a typed RPC request on the worker."""
        result = await self.call(
            request.namespace,
            request.method,
            request.to_args(),
            timeout=timeout,
        )
        return type(request).parse_response(result)

    async def reserve(self, timeout: float | None = None) -> ReservedWorker:
        """Acquire an exclusive worker lease for an EvalSession."""
        if self._proc is None:
            raise WorkerDiedError("Worker not started")
        if self._available.locked():
            if timeout is None:
                raise WorkerBusyError("worker is busy")
            try:
                await asyncio.wait_for(self._available.acquire(), timeout=timeout)
            except TimeoutError as exc:
                raise WorkerBusyError(f"worker is busy after waiting {timeout}s") from exc
        else:
            await self._available.acquire()
        return ReservedWorker(self)

    def _release(self) -> None:
        """Release the worker lock (called by ReservedWorker.release())."""
        self._available.release()

    # ── log access ─────────────────────────────────────────────────

    async def log_stream(self):
        """Async iterator over log events."""
        q: asyncio.Queue = asyncio.Queue()

        def _on_event(event: object) -> None:
            q.put_nowait(event)

        sub = self._log_bus.subscribe(_on_event)
        try:
            while True:
                event = await q.get()
                if event is None:
                    break
                yield event
        finally:
            sub.unsubscribe()

    def subscribe(self, callback) -> _Subscription:
        """Subscribe a callback to all log events.

        Callback receives dict: ``{"request_id": N, "action": ..., "args": [...]}``.
        Returns a ``_Subscription`` — call ``.unsubscribe()`` to stop.
        """
        return self._log_bus.subscribe(callback)
