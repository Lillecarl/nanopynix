# pynixd Code Analysis — Top 10 Improvements

**Date:** 2026-03-23
**Scope:** All Python files in `pynixd/` — `__init__.py`, `__main__.py`, `wire.py`, `connection.py`, `proxy.py`, `scheduler.py`, `build_queue.py`, `store.py`, `stderr.py`, `protocol.py`, `drv_parser.py`, `exceptions.py`, `local_store_db.py`, `http_cache.py`, `ssh_server.py`, `unix_server.py`, `operations/` (all 8 modules)

---

## Top 10 Prioritized Improvements

### 1. Orphaned futures in `build_queue.py` — client hangs
**Files:** `build_queue.py:154-178`

When a second `enqueue()` arrives for a derivation already building, a new future is created and `_by_key[key]` is overwritten. The original build continues but its future is never resolved — the waiting client hangs forever. This is a **correctness bug** that causes builds to silently disappear.

**Fix:** In-progress builds should either be returned directly (not replaced) or the new future must resolve when the original completes.

---

### 2. `_known_paths` not updated on SQLite fast-path
**Files:** `proxy.py:264,270,276`; `store.py:150-156`

The fast-path for `IsValidPath`/`QueryPathInfo`/`QueryValidPaths` calls `db.mark_path()` directly, bypassing `Store.add_known_path()`. The in-memory `_known_paths` set is never updated. The scheduler's locality scoring (`scheduler.py:145`) then returns `False` for paths that were just marked, starving the locality heuristic and causing unnecessary remote transfers.

**Fix:** Replace `db.mark_path()` calls in the fast-path with `self._local_store.add_known_path()`.

---

### 3. `NarFromPath` protocol violation for missing paths
**Files:** `proxy.py:218-219`; `operations/queries.py:190-191`

When a path is not in the local store, `NarFromPathResponse(nar_data=b"")` writes **zero bytes** to the client. The Nix daemon protocol requires a `nar_size` uint64 prefix followed by zero NAR bytes. A strict client can reject or misparse this. The valid-path path correctly writes `nar_size` via `nar_from_path_to_writer`.

**Fix:** Write `uint64(0)` before an empty `nar_data` for missing paths.

---

### 4. `ssh_server.py` always exits 0, masking build failures
**File:** `ssh_server.py:116-119`

The `finally` block unconditionally calls `process.exit(0)` regardless of whether `proxy.run()` succeeded or raised an exception. The nix client receives success exit code even when the proxy crashed internally. Additionally, at lines 102-105, `process.exit(1)` is immediately followed by `return`, so the `finally` overrides it and the shell also receives 0.

**Fix:** Exit with non-zero code when `proxy.run()` raises; remove the `return` or restructure the finally.

---

### 5. SSH subprocesses never terminated on close
**File:** `store.py:850-851`

`SSHSubprocessStore.close()` calls `proc.close()` which only releases the handle — it does **not** send SIGTERM to the remote `nix-daemon --stdio` process. Compare to `LocalSubprocessStore.close()` (line 709-714) which calls `proc.terminate()` then `await proc.wait()`. SSH daemons leak indefinitely on every store restart.

**Fix:** Call `proc.terminate()` + `await proc.wait()` in `SSHSubprocessStore.close()`.

---

### 6. `Connection.close()` is a no-op — socket leak
**File:** `connection.py:89-91`

`close()` only flips `self.connected = False`. Neither the underlying `AsyncReader`/`AsyncWriter` nor the `DaemonReader`/`DaemonWriter` wrappers are closed. The transport sockets are never released. Callers (e.g. `store.py` connection pool) have no way to actually close connections, leading to socket exhaustion in long-running processes.

**Fix:** Actually close the transport writer, or document that callers must close the transport directly.

---

### 7. `_forward_framed_snooping` writes EOF before validating buffer
**File:** `operations/store_mutations.py:319-323`

When EOF is detected, the terminating zero is written to `dst` **before** checking whether the parse buffer satisfies the pending `_ensure(n)` byte request. If `len(buf) < n`, an `EOFError` is raised **after** the terminator is already forwarded, corrupting the client's stream with a premature EOF marker.

**Fix:** Check buffer bounds before writing the terminator.

---

### 8. `_flush_loop` dies silently on exception — DB staleness
**File:** `local_store_db.py:256-262`

If `flush_regtime()` raises any non-cancellation exception, the background flush loop terminates permanently. Any paths added via `mark_paths()` after that point are **never flushed to SQLite** until `start()` is called again. The DB becomes increasingly stale relative to actual store contents, causing `QueryValidPaths` to return incomplete results.

**Fix:** Catch exceptions in `_flush_loop` and restart the loop, or propagate to the caller.

---

### 9. `.drv` files silently skipped on `FileNotFoundError`
**File:** `drv_parser.py:489-490,513-514,533-534,559-560,605-608`

`collect_required_paths`, `collect_output_paths`, `to_basic_derivation`, and `extract_platforms` all silently catch `FileNotFoundError` and continue. Missing referenced derivations produce incomplete path sets. Downstream builds fail with mysterious "missing input" errors. No distinction is made between "file legitimately absent" and "build graph broken."

**Fix:** Either raise a structured exception on missing inputs, or at minimum track and report which paths were skipped.

---

### 10. `_pull_paths` runs sequential queries instead of concurrent
**File:** `scheduler.py:669-679`

```python
for path in paths:
    path_info = await store.query_path_info(path)  # sequential
```

50 paths = 50 sequential network round-trips. This is the entire path transfer bottleneck on multi-path transfers. Should use `asyncio.gather` to overlap latency.

**Fix:** `asyncio.gather(*[store.query_path_info(p) for p in paths])`.

---

## Runners-Up (also significant)

| # | Issue | File(s) | Impact |
|---|-------|---------|--------|
| 11 | `_known_paths` accessed without synchronization | `store.py:89,142,145,148,151,155,156,425,431` | Data races under concurrent load |
| 12 | `copy_paths` doesn't read per-request responses | `store.py:238-249` | Wrong response data consumed on pipelining |
| 13 | `_sweep_idle` dies on exception, stops cleanup | `store.py:475,490` | Stale connections accumulate forever |
| 14 | Duplicated NAR parsing in `copy_nar`/`discard_nar` | `wire.py:232-255,268-297` | Protocol desync risk |
| 15 | `SSHSubprocessStore` process orphaned on failed `conn.connect()` | `store.py:841` | Leaked SSH processes per failed connection |
| 16 | `set()` iteration in `write_string_set` is non-deterministic | `wire.py:151-154` | Wire sessions irreproducible, caching breaks |
| 17 | `query_valid_paths` is N separate queries | `local_store_db.py:169-182` | O(n) SQL round-trips, should be single `IN` query |
| 18 | `stderr.forward` `StderrError` not forwarded before raising | `stderr.py:354-358` | Client sees incomplete stderr on backend errors |
| 19 | `BackendError` caught and returns `None` silently | `proxy.py:230-233` | Client times out instead of receiving error |
| 20 | `NarFromPath` re-reads .drv and discards PathInfo | `proxy.py:206-209,389-405` | Redundant file I/O per build |
