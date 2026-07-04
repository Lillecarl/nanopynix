"""Subprocess worker pool — Nix execution backend via multiprocessing.

Each subprocess is an independent Nix process with its own Store, logger,
and globals.  Workers use the ``forkserver`` start method for COW memory
sharing.  Communication via ``multiprocessing.Pipe`` (pickled dicts).
"""

from __future__ import annotations

import asyncio
import multiprocessing as _mp
import time
import traceback

try:
    _mp.set_start_method("forkserver")
except RuntimeError:
    pass  # already set by outer process (e.g. pytest)

from nanopynix.exceptions import from_response

# ────────────────────────────────────────────────────────────────────
_RPC_TIMEOUT = 300.0


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
        "_proc", "_req_conn", "_resp_conn", "req_id_base", "_next_id",
        "_responses", "_events", "_done", "_dead", "_timeout", "_last_used",
        "_last_activity", "_read_task",
    )

    def __init__(
        self,
        proc: _mp.Process,
        req_conn,
        resp_conn,
        req_id_base: int,
        timeout: float = _RPC_TIMEOUT,
    ) -> None:
        self._proc = proc
        self._req_conn = req_conn
        self._resp_conn = resp_conn
        self.req_id_base = req_id_base
        self._next_id = 0
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

    def next_id(self) -> int:
        rid = self.req_id_base | self._next_id
        self._next_id += 1
        return rid

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
            # Structured error dict from worker: {error_type, msg, traceback, ...}
            raise from_response(
                error_type=payload.get("error_type", "Unknown"),
                msg=payload.get("msg", "unknown"),
                raw=payload.get("traceback", ""),
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
        if self._read_task is not None:
            try:
                await asyncio.wait_for(self._read_task, timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                self._read_task.cancel()


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
        self._worker_id_counter = 0

    @property
    def rpc_timeout(self) -> float:
        return self._rpc_timeout

    async def open(self) -> None:
        """Spawn initial workers."""
        for _ in range(self._max_workers):
            await self._spawn()

    async def close(self) -> None:
        for w in self._workers:
            await w.close()
        self._workers.clear()
        while not self._free.empty():
            self._free.get_nowait()
        for task in self._relay_tasks:
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                task.cancel()
        self._relay_tasks.clear()

    async def _spawn(self) -> _WorkerRef:
        wid = self._worker_id_counter
        self._worker_id_counter += 1

        # Req pipe: parent → child.  conn2 is the send end.
        req_child_recv, req_parent_send = _mp.Pipe(duplex=False)
        # Resp pipe: child → parent.  conn2 is the send end.
        resp_parent_recv, resp_child_send = _mp.Pipe(duplex=False)

        import nanopynix._worker as _worker_module

        proc = _mp.Process(
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
            raise RuntimeError(f"Worker {wid} init failed: {ready}")

        req_id_base = wid << 48
        worker = _WorkerRef(proc, req_parent_send, resp_parent_recv, req_id_base, timeout=self._rpc_timeout)

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

    async def _send_recv(self, module: str, fn: str, args: list, timeout: float | None = None) -> dict:
        worker = await self._acquire()
        try:
            return await worker.send_recv(module, fn, args, timeout=timeout)
        finally:
            await self._release(worker)

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
        while True:
            event = await self._log_events.get()
            if event is None:
                break
            yield event

    # ── Store methods ──────────────────────────────────────────

    async def store_get_uri(self) -> str:
        return await self._send_recv("store", "get_uri", [])

    async def store_get_store_dir(self) -> str:
        return await self._send_recv("store", "get_store_dir", [])

    async def store_is_valid_path(self, path_str: str) -> bool:
        return await self._send_recv("store", "is_valid_path", [path_str])

    async def store_parse_store_path(self, path_str: str) -> dict:
        return await self._send_recv("store", "parse_store_path", [path_str])

    async def store_query_path_info(self, path_str: str) -> dict:
        return await self._send_recv("store", "query_path_info", [path_str])

    async def store_query_path_from_hash_part(self, hash_part: str) -> dict:
        return await self._send_recv("store", "query_path_from_hash_part", [hash_part])

    async def store_compute_fs_closure(
        self, path_str: str, flip: bool = False,
        include_outputs: bool = False, include_derivers: bool = False,
    ) -> list[dict]:
        return await self._send_recv("store", "compute_fs_closure",
            [path_str, flip, include_outputs, include_derivers])

    async def store_query_missing(self, paths: list[str]) -> dict:
        return await self._send_recv("store", "query_missing", [paths])

    async def store_query_derivation_outputs(self, path_str: str) -> list[dict]:
        return await self._send_recv("store", "query_derivation_outputs", [path_str])

    async def store_query_valid_derivers(self, path_str: str) -> list[dict]:
        return await self._send_recv("store", "query_valid_derivers", [path_str])

    async def store_query_all_valid_paths(self) -> list[dict]:
        return await self._send_recv("store", "query_all_valid_paths", [])

    async def store_query_referrers(self, path_str: str) -> list[dict]:
        return await self._send_recv("store", "query_referrers", [path_str])

    async def store_query_substitutable_paths(self, paths: list[str]) -> list[dict]:
        return await self._send_recv("store", "query_substitutable_paths", [paths])

    async def store_build_paths_with_results(self, paths: list[str]) -> list[dict]:
        return await self._send_recv("store", "build_paths_with_results", [paths])

    async def store_read_derivation(self, drv_path: str) -> dict:
        return await self._send_recv("store", "read_derivation", [drv_path])

    async def store_build_derivation(self, drv_path: str, build_mode: int) -> dict:
        return await self._send_recv("store", "build_derivation", [drv_path, build_mode])

    async def store_follow_links_to_store_path(self, path_str: str) -> dict:
        return await self._send_recv("store", "follow_links_to_store_path", [path_str])

    async def store_add_temp_root(self, path_str: str) -> None:
        await self._send_recv("store", "add_temp_root", [path_str])

    # ── Fetchers ───────────────────────────────────────────────

    async def fetchers_input_from_url(self, url: str) -> dict:
        return await self._send_recv("fetchers", "input_from_url", [url])

    async def fetchers_input_from_attrs(self, attrs: dict[str, str]) -> dict:
        return await self._send_recv("fetchers", "input_from_attrs", [list(attrs.items())])
