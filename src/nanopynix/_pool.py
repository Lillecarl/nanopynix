"""Subprocess worker pool — Nix execution backend via multiprocessing.

Each subprocess is an independent Nix process with its own Store, logger,
and globals.  Workers use the ``forkserver`` start method for COW memory
sharing.  Communication via ``multiprocessing.Pipe`` (pickled dicts).
"""

from __future__ import annotations

import asyncio
import itertools
import multiprocessing as _mp
import time
import traceback

_mp_ctx = _mp.get_context("forkserver")

from nanopynix.exceptions import from_response

# ────────────────────────────────────────────────────────────────────
_RPC_TIMEOUT = 300.0
_id_counter = itertools.count()


# ════════════════════════════════════════════════════════════════════
# Exceptions
# ════════════════════════════════════════════════════════════════════

class WorkerDied(RuntimeError):
    """Raised when a subprocess worker dies unexpectedly."""


# ════════════════════════════════════════════════════════════════════
# _WorkerRef — handle to a single subprocess worker
# ════════════════════════════════════════════════════════════════════

class _WorkerRef:
    """Handle to a live subprocess worker communicating via mp.Pipe."""

    __slots__ = (
        "_proc", "_req_conn", "_resp_conn",
        "_responses", "_events", "_done", "_dead", "_timeout", "_last_used",
        "_last_activity", "_read_task",
    )

    def __init__(
        self,
        proc: _mp_ctx.Process,
        req_conn,
        resp_conn,
        timeout: float = _RPC_TIMEOUT,
    ) -> None:
        self._proc = proc
        self._req_conn = req_conn
        self._resp_conn = resp_conn
        self._responses: asyncio.Queue = asyncio.Queue()
        self._events: asyncio.Queue = asyncio.Queue()
        self._done = False
        self._dead: asyncio.Event = asyncio.Event()
        self._timeout = timeout
        self._last_used = time.monotonic()
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
            self._events.put_nowait(None)  # unblock _relay_events

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

        t = self._timeout if timeout is None else timeout
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
                raise TimeoutError(f"Call timed out — no worker activity for {t}s")

            # Poll with short timeout so we can check for activity frequently
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
            # Structured error dict from worker: {error_type, msg, traceback, info, ...}
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
    """Exclusive lease on a pool worker, obtained via ``WorkerPool.reserve()``.

    Delegates ``send_recv`` to the underlying ``_WorkerRef`` and returns
    the worker to the pool on ``release()``.
    """

    __slots__ = ("_pool", "worker", "_released")

    def __init__(self, pool: WorkerPool, worker: _WorkerRef) -> None:
        self._pool = pool
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
        """Return the worker to the pool.  Idempotent — safe to call twice."""
        if not self._released:
            self._released = True
            await self._pool._release(self.worker)


# ════════════════════════════════════════════════════════════════════
# WorkerPool
# ════════════════════════════════════════════════════════════════════

class WorkerPool:
    """Pool of subprocess workers, each with an independent Nix Store."""

    def __init__(
        self,
        max_workers: int = 4,
        *,
        store_uri: str = "auto",
        eval_store_uri: str | None = None,
        settings: dict[str, str] | None = None,
        experimental_features: list[str] | None = None,
        rpc_timeout: float = _RPC_TIMEOUT,
        idle_timeout: float | None = None,
    ) -> None:
        self._max_workers = max_workers
        self._store_uri = store_uri
        self._eval_store_uri = eval_store_uri or store_uri
        self._settings = settings or {}
        self._features = experimental_features or []
        self._rpc_timeout = rpc_timeout
        self._idle_timeout = idle_timeout
        self._workers: list[_WorkerRef] = []
        self._free: asyncio.Queue[_WorkerRef] = asyncio.Queue()
        self._log_events: asyncio.Queue = asyncio.Queue()
        self._relay_tasks: list[asyncio.Task] = []
        self._log_done: asyncio.Event = asyncio.Event()
        self._worker_id_counter = 0

    @property
    def rpc_timeout(self) -> float:
        return self._rpc_timeout

    async def open(self) -> None:
        """Spawn initial workers."""
        for _ in range(self._max_workers):
            await self._spawn()

    async def close(self) -> None:
        # Close all workers concurrently — one stuck worker shouldn't block others
        results = await asyncio.gather(
            *(w.close() for w in self._workers),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                import traceback
                traceback.print_exception(type(r), r, r.__traceback__)
        self._workers.clear()
        while not self._free.empty():
            self._free.get_nowait()
        for task in self._relay_tasks:
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                task.cancel()
        self._relay_tasks.clear()
        self._log_done.set()
        self._log_events.put_nowait(None)  # unblock log_stream() if waiting

    async def _spawn(self) -> _WorkerRef:
        wid = self._worker_id_counter
        self._worker_id_counter += 1

        # Req pipe: parent → child.  conn2 is the send end.
        req_child_recv, req_parent_send = _mp_ctx.Pipe(duplex=False)
        # Resp pipe: child → parent.  conn2 is the send end.
        resp_parent_recv, resp_child_send = _mp_ctx.Pipe(duplex=False)

        import nanopynix._worker as _worker_module

        args = [req_child_recv, resp_child_send]

        proc = _mp_ctx.Process(
            target=_worker_module.main,
            args=tuple(args),
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
            raise RuntimeError(f"Worker {wid} init failed: {ready}")

        worker = _WorkerRef(proc, req_parent_send, resp_parent_recv, timeout=self._rpc_timeout)

        worker._read_task = asyncio.ensure_future(worker._read_responses())
        self._relay_tasks.append(asyncio.ensure_future(self._relay_events(worker)))

        self._workers.append(worker)
        await self._free.put(worker)
        return worker

    async def _relay_events(self, worker: _WorkerRef) -> None:
        while True:
            msg = await worker._events.get()
            if msg is None:
                break  # _read_responses exited
            await self._log_events.put(msg)

    async def reserve(self) -> ReservedWorker:
        """Acquire an exclusive worker lease from the pool.

        Returns a ``ReservedWorker`` that must be released via ``.release()``.
        Used internally by ``_send_recv`` (single-call acquire→release) and
        externally by ``EvalSession`` (multi-call exclusive lease).
        """
        worker = await self._acquire()
        return ReservedWorker(self, worker)

    async def call(self, module: str, fn: str, args: list, *, timeout: float | None = None) -> dict:
        """Send an RPC call on any free worker and return the response.

        This is the general-purpose entry point for all RPC operations.
        Acquires a worker, sends the call, releases the worker.
        """
        return await self._send_recv(module, fn, args, timeout=timeout)

    async def _send_recv(self, module: str, fn: str, args: list, timeout: float | None = None) -> dict:
        rw = await self.reserve()
        try:
            return await rw.send_recv(module, fn, args, timeout=timeout)
        finally:
            await rw.release()

    async def _acquire(self) -> _WorkerRef:
        while True:
            worker = await self._free.get()
            idle = time.monotonic() - worker._last_used
            if self._idle_timeout is not None and idle > self._idle_timeout:
                await worker.close()
                self._workers.remove(worker)
                try:
                    new = await self._spawn()
                except Exception:
                    continue
                return new
            return worker

    async def _release(self, worker: _WorkerRef) -> None:
        worker._last_used = time.monotonic()
        await self._free.put(worker)

    async def log_stream(self):
        while not self._log_done.is_set():
            event = await self._log_events.get()
            if event is None:
                break
            yield event
