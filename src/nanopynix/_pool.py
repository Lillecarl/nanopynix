"""Subprocess worker — Nix execution backend via multiprocessing.

A single subprocess runs an independent Nix process with its own Store,
logger, and globals.  Uses the ``forkserver`` start method for COW
memory sharing.  Communication via ``multiprocessing.Pipe`` (pickled dicts).

Only one call is in-flight at a time — the worker is single-threaded.
``_WorkerManager.call()`` acquires the worker, sends a call, and releases
it.  ``_WorkerManager.reserve()`` acquires the worker exclusively for
the duration of an ``EvalSession``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import multiprocessing as _mp
import time
import traceback

_mp_ctx = _mp.get_context("forkserver")

from nanopynix.exceptions import from_response

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
_RPC_TIMEOUT = 300.0
_id_counter = itertools.count()


# ════════════════════════════════════════════════════════════════════
# Exceptions
# ════════════════════════════════════════════════════════════════════

class WorkerDied(RuntimeError):
    """Raised when the subprocess worker dies unexpectedly."""


# ════════════════════════════════════════════════════════════════════
# _WorkerRef — handle to the subprocess worker
# ════════════════════════════════════════════════════════════════════

class _WorkerRef:
    """Handle to a live subprocess worker communicating via mp.Pipe."""

    __slots__ = (
        "_proc", "_req_conn", "_resp_conn",
        "_responses", "_events", "_done", "_dead",
        "_last_activity", "_read_task",
    )

    def __init__(
        self,
        proc: _mp_ctx.Process,
        req_conn,
        resp_conn,
    ) -> None:
        self._proc = proc
        self._req_conn = req_conn
        self._resp_conn = resp_conn
        self._responses: asyncio.Queue = asyncio.Queue()
        self._events: asyncio.Queue = asyncio.Queue()
        self._done = False
        self._dead: asyncio.Event = asyncio.Event()
        self._last_activity = time.monotonic()
        self._read_task: asyncio.Task | None = None

    @property
    def is_dead(self) -> bool:
        return self._dead.is_set() or not self._proc.is_alive()

    @staticmethod
    def next_id() -> int:
        return next(_id_counter)

    async def _read_responses(self) -> None:
        """Background task: drain the response pipe into asyncio queues."""
        loop = asyncio.get_running_loop()
        try:
            while not self._done:
                msg = await loop.run_in_executor(None, self._resp_conn.recv)
                self._last_activity = time.monotonic()
                t = msg.get("type")
                if t == "result":
                    await self._responses.put(("ok", msg.get("id"), msg.get("value", {})))
                elif t == "error":
                    await self._responses.put(("err", msg.get("id"), msg))
                elif t == "event":
                    await self._events.put(msg)
        except (EOFError, OSError, BrokenPipeError):
            pass
        except Exception:
            traceback.print_exc()
        finally:
            self._dead.set()
            self._events.put_nowait(None)  # unblock relay

    async def send_recv(self, module: str, fn: str, args: list, timeout: float | None = None) -> dict:
        """Send a call and wait for the matching response.

        The timeout is an *idle* timeout: it resets whenever the worker
        sends any message (log event, response, etc.), so long-running
        operations like builds don't time out while the worker is active.

        Raises:
            WorkerDied: the worker process died.
            TimeoutError: *timeout* seconds elapsed with no activity from the worker.
            RuntimeError: the worker returned an error.
        """
        if self.is_dead:
            raise WorkerDied("Worker is dead")

        t = _RPC_TIMEOUT if timeout is None else timeout
        req_id = self.next_id()
        loop = asyncio.get_running_loop()

        # Phase 1: send the request (fixed timeout — send should be fast)
        async with asyncio.timeout(t):
            await loop.run_in_executor(None, self._req_conn.send, {
                "type": "call",
                "id": req_id,
                "module": module,
                "fn": fn,
                "args": args,
            })

        # Phase 2: wait for response with idle timeout that resets on activity
        last_seen = self._last_activity
        while True:
            remaining = t - (time.monotonic() - last_seen)
            if remaining <= 0:
                # Check for a response that arrived during the race window
                try:
                    kind, rid, payload = self._responses.get_nowait()
                except asyncio.QueueEmpty:
                    raise TimeoutError(f"Call timed out — no worker activity for {t}s")
                # Got a late response — process it
                if rid == req_id:
                    if kind == "ok":
                        return payload
                    raise from_response(
                        error_type=payload.get("error_type", "Unknown"),
                        msg=payload.get("msg", "unknown"),
                        raw=payload.get("traceback", ""),
                        info=payload.get("info"),
                    )
                # Response for a different request — reset and continue
                last_seen = self._last_activity
                continue

            try:
                async with asyncio.timeout(min(remaining, 1.0)):
                    get_task = asyncio.ensure_future(self._responses.get())
                    dead_task = asyncio.ensure_future(self._dead.wait())
                    done, pending = await asyncio.wait(
                        [get_task, dead_task], return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
            except asyncio.TimeoutError:
                # No response arrived within the poll window —
                # check if the worker sent anything (event, etc.)
                if self._last_activity > last_seen:
                    last_seen = self._last_activity
                continue

            if dead_task in done:
                raise WorkerDied("Worker process died")

            kind, rid, payload = get_task.result()
            if rid != req_id:
                # Response for a different request — worker is alive, reset
                last_seen = self._last_activity
                continue
            if kind == "ok":
                return payload
            # Structured error dict from worker
            raise from_response(
                error_type=payload.get("error_type", "Unknown"),
                msg=payload.get("msg", "unknown"),
                raw=payload.get("traceback", ""),
                info=payload.get("info"),
            )

    async def close(self) -> None:
        self._done = True
        try:
            loop = asyncio.get_running_loop()
            async with asyncio.timeout(2.0):
                await loop.run_in_executor(None, self._req_conn.send, {"type": "close"})
        except (TimeoutError, Exception):
            pass
        try:
            self._proc.join(timeout=2)
        except Exception:
            pass
        if self._proc.is_alive():
            self._proc.kill()
            self._proc.join()
        # Close pipes to unblock any executor thread blocked on recv()
        try:
            self._req_conn.close()
        except Exception:
            pass
        try:
            self._resp_conn.close()
        except Exception:
            pass
        if self._read_task is not None:
            try:
                await asyncio.wait_for(self._read_task, timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                self._read_task.cancel()


# ════════════════════════════════════════════════════════════════════
# ReservedWorker — public token for an exclusive worker lease
# ════════════════════════════════════════════════════════════════════

class ReservedWorker:
    """Exclusive lease on the session worker, obtained via ``_WorkerManager.reserve()``.

    Delegates ``send_recv`` to the underlying ``_WorkerRef`` and
    releases the worker back to the manager on ``release()``.
    """

    __slots__ = ("_manager", "worker", "_released")

    def __init__(self, manager: _WorkerManager, worker: _WorkerRef) -> None:
        self._manager = manager
        self.worker = worker
        self._released = False

    async def send_recv(
        self, module: str, fn: str, args: list, timeout: float | None = None,
    ) -> dict:
        """Send an RPC call on the reserved worker and await the response."""
        if self._released:
            raise RuntimeError("ReservedWorker has been released")
        return await self.worker.send_recv(module, fn, args, timeout=timeout)

    async def release(self) -> None:
        """Return the worker to the manager.  Idempotent — safe to call twice."""
        if not self._released:
            self._released = True
            self._manager._release()


# ════════════════════════════════════════════════════════════════════
# _WorkerManager — single-worker lifecycle
# ════════════════════════════════════════════════════════════════════

class _LogBus:
    """Thread-safe subscriber list for worker log events.

    Zero subscribers → events are discarded (no buffering, no overhead).
    Callbacks are called synchronously from the relay task — keep them fast.
    """

    def __init__(self) -> None:
        self._subscribers: list = []  # list of callbacks

    def subscribe(self, callback) -> _Subscription:
        """Register a callback.  Returns a handle for unsubscribe."""
        self._subscribers.append(callback)
        return _Subscription(self, callback)

    def _unsubscribe(self, sub: _Subscription) -> None:
        try:
            self._subscribers.remove(sub._callback)
        except ValueError:
            pass

    def emit(self, event: object) -> None:
        """Deliver an event to all subscribers.  No-op if none."""
        if not self._subscribers:
            return
        for cb in self._subscribers:
            try:
                cb(event)
            except Exception:
                pass  # one bad subscriber shouldn't break others


class _Subscription:
    """Handle returned by ``_LogBus.subscribe()``."""

    __slots__ = ("_bus", "_callback")

    def __init__(self, bus: _LogBus, callback) -> None:
        self._bus = bus
        self._callback = callback

    def unsubscribe(self) -> None:
        """Remove this subscriber from the bus.  Idempotent."""
        self._bus._unsubscribe(self)


class _WorkerManager:
    """Manages a single subprocess worker with an independent Nix Store.

    Provides:
    - ``call()`` — acquire→send→release for individual RPC calls (used by Store).
    - ``reserve()`` — exclusive worker lease (used by EvalSession).
    - ``log_stream()`` — async iterator over log events from the worker.
    """

    def __init__(
        self,
        *,
        store_uri: str = "auto",
        eval_store_uri: str | None = None,
        settings: dict[str, str] | None = None,
        experimental_features: list[str] | None = None,
    ) -> None:
        self._store_uri = store_uri
        self._eval_store_uri = eval_store_uri or store_uri
        self._settings = settings or {}
        self._features = experimental_features or []
        self._worker: _WorkerRef | None = None
        self._available: asyncio.Lock = asyncio.Lock()
        self._log_bus: _LogBus = _LogBus()
        self._relay_task: asyncio.Task | None = None
        self._log_done: asyncio.Event = asyncio.Event()

    async def open(self) -> None:
        """Spawn the worker subprocess."""
        self._worker = await self._spawn()

    async def close(self) -> None:
        """Shut down the worker."""
        if self._worker is not None:
            await self._worker.close()
            self._worker = None
        if self._relay_task is not None:
            try:
                await asyncio.wait_for(self._relay_task, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                self._relay_task.cancel()
        self._log_done.set()
        self._log_bus.emit(None)  # unblock log_stream() if waiting

    async def _spawn(self) -> _WorkerRef:
        req_child_recv, req_parent_send = _mp_ctx.Pipe(duplex=False)
        resp_parent_recv, resp_child_send = _mp_ctx.Pipe(duplex=False)

        import nanopynix._worker as _worker_module

        proc = _mp_ctx.Process(
            target=_worker_module.main,
            args=(req_child_recv, resp_child_send),
            daemon=True,
        )
        proc.start()

        # Parent closes child ends
        req_child_recv.close()
        resp_child_send.close()

        # Init handshake
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, req_parent_send.send, {
            "type": "init",
            "store_uri": self._store_uri,
            "eval_store_uri": self._eval_store_uri,
            "settings": self._settings,
            "experimental_features": self._features,
        })
        ready = await loop.run_in_executor(None, resp_parent_recv.recv)
        if ready.get("type") != "ready":
            proc.kill()
            proc.join(timeout=2)
            req_parent_send.close()
            resp_parent_recv.close()
            raise RuntimeError(f"Worker init failed: {ready}")

        worker = _WorkerRef(proc, req_parent_send, resp_parent_recv)
        worker._read_task = asyncio.ensure_future(worker._read_responses())
        self._relay_task = asyncio.ensure_future(self._relay_events(worker))
        return worker

    async def _relay_events(self, worker: _WorkerRef) -> None:
        while True:
            msg = await worker._events.get()
            if msg is None:
                break  # _read_responses exited
            self._log_bus.emit(msg)

    # ── public API ─────────────────────────────────────────────────

    async def call(self, module: str, fn: str, args: list, *, timeout: float | None = None) -> dict:
        """Send an RPC call on the worker and return the response.

        Acquires the worker lock — only one call in-flight at a time.
        Concurrent callers queue up on the lock.
        """
        if self._worker is None:
            raise WorkerDied("Worker not started")
        async with self._available:
            return await self._worker.send_recv(module, fn, args, timeout=timeout)

    async def reserve(self) -> ReservedWorker:
        """Acquire an exclusive worker lease.

        Returns a ``ReservedWorker`` that must be released via ``.release()``.
        Blocks until the worker is available.
        """
        if self._worker is None:
            raise WorkerDied("Worker not started")
        await self._available.acquire()
        return ReservedWorker(self, self._worker)

    def _release(self) -> None:
        """Release the worker back to the manager (called by ReservedWorker.release())."""
        self._available.release()

    async def log_stream(self):
        """Async iterator over log events.

        Internally subscribes to the log bus with a private queue.
        Unsubscribes automatically when iteration ends.
        """
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

        The callback receives raw event dicts from the worker
        (``{\"type\": \"event\", \"id\": req_id, \"action\": ..., \"args\": [...]}``).

        Returns a ``_Subscription`` — call ``.unsubscribe()`` to stop.
        """
        return self._log_bus.subscribe(callback)
