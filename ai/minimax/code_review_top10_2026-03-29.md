# pynixd Code Review — Top 10 Issues
**Date:** 2026-03-29
**Reviewers:** 5 subagents (Naming/Org, Async/Concurrency, Error Handling, Data Flow/Types, Test Quality)

---

## Top 10 Issues (Ordered by Impact)

### 1. `drv_parser.py:644` — Blocking `read_drv_file()` blocks the async event loop
`open(fs_path)` / `f.read()` is synchronous file I/O called on every `BuildDerivation` request from `_enrich_derivation` (`proxy.py:423`) and `_decompose_build_paths` (`proxy.py:469`). Under load this freezes the event loop for all concurrent clients. **Fix:** Replace with `aiofiles` or `run_in_executor`.

### 2. `scheduler.py:350` (`_retry_build`) — Orphaned proactive transfer task races after retry
After setting `build.transfer_task = None`, the existing `_do_proactive_transfer` task is never cancelled or awaited. Its `finally: self.trigger()` races with new scheduling passes, potentially starting duplicate transfers for the same build. **Fix:** Call `build.stop_transfer()` before resetting state.

### 3. `http_cache.py:202-212` — HTTP streaming failures return truncated 200 OK
After `response.prepare()` commits HTTP 200, any streaming failure returns a truncated NAR with no error indication. Clients receive incomplete bytes and believe the download succeeded (a 1GB NAR failing at 500MB looks like success). **Fix:** Check `response.prepared` before streaming, or close the connection on failure.

### 4. `scheduler.py:702-706` — `_pull_paths` silently returns when all queries fail
When all `query_one` calls fail, `to_pull` is empty and `_pull_paths` returns silently. The caller proceeds as if zero paths were needed, and `BuildDerivationResponse` returns success with non-existent output paths. **Fix:** Raise an exception or return a failure indicator when all path queries fail.

### 5. `scheduler.py:538-558` — Proactive transfer failures don't trigger rescheduling
Exceptions in `_do_proactive_transfer` are logged but `self.trigger()` is never called. Builds remain in scheduler limbo — unscheduled but not failed. **Fix:** Call `self.trigger()` in the outer exception handler.

### 6. `scheduler.py:154-166` — `_sync_local_paths` silently proceeds on exception
If `query_valid_paths` fails, the scheduler proceeds assuming paths don't exist, causing incorrect DAG scheduling decisions (builds that could proceed wait for dependencies). **Fix:** Treat path uncertainty explicitly rather than silently falling through.

### 7. `store.py:1` — 1246-line god module
`Store` ABC, four concrete subclasses (`LocalSubprocessStore`, `LocalSocketStore`, `SSHSubprocessStore`, `SSHSocketStore`), and `_SSHStoreMixin` all in one file. Transport mechanics are entangled with connection pooling, circuit breakers, and NAR streaming. **Fix:** Split into `store/base.py`, `store/local_daemon.py`, `store/local_subprocess.py`, `store/ssh.py`, `store/mixins.py`.

### 8. `queries.py:56-71` — Redundant `valid`/`info` fields in `QueryPathInfoResponse`
`valid: bool` and `info: PathInfo | None` always agree — one is derivable from the other. This redundancy creates a consistency hazard if the two fields ever diverge. **Fix:** Remove `valid`, derive it as `info is not None`.

### 9. `store.py:295-336` — Six streaming methods use `conn.dirty = True; raise` pattern
Each streaming method sets `conn.dirty = True` in its except clause. If any caller forgets the dirty flag, a corrupted connection returns to the pool silently. **Fix:** Use a context manager or `__aexit__` that always marks dirty on exception.

### 10. `test_bench_build.py:124-165` — Benchmark test has zero assertions
`test_build_throughput` records elapsed time but never asserts build success or performance threshold. A completely broken build passes this test. **Fix:** Add `assert result.returncode == 0` and a performance assertion.

---

## Full Critical Findings

### Async/Concurrency
- **`drv_parser.py:644`** — Blocking `open()`/`f.read()` on event loop (see #1 above)
- **`scheduler.py:350`** — Orphaned proactive transfer task (see #2 above)
- **`store.py:755, 1120, 1143`** — Blocking `os.makedirs`/`os.path.exists` inside async coroutines
- **`store.py:251`** — Both transfer semaphores held across full batch in `stream_paths_store_to_store`, creating convoy effect
- **`gc.py:64`** — GC loop discards work on cancellation without flushing (inconsistent with `local_store_db.py:406`)

### Error Handling
- **`http_cache.py:202-212`** — Truncated 200 OK (see #3 above)
- **`scheduler.py:702-706`** — Silent `None` return from `_pull_paths` (see #4 above)
- **`scheduler.py:538-558`** — Proactive transfer failures leave builds in limbo (see #5 above)
- **`scheduler.py:154-166`** — Silent degradation on `_sync_local_paths` exception (see #6 above)
- **`connection.py:76-92`** — `_drain_loop` doesn't handle `OSError` on broken connection; drain task crashes and stderr stops forwarding
- **`store.py:295-336`** — `conn.dirty = True` pattern (see #9 above)

### Data Flow/Types
- **`queries.py:56-71`** — Redundant `valid`/`info` fields (see #8 above)
- **`base.py:602-614`** — `BuildResult` has untyped `is_non_deterministic` (int not bool), `start_time`, `stop_time`; `built_outputs: dict[str, dict]` bypasses `BuiltOutput` dataclass
- **`base.py:256-283`** — `PathInfo.nar_hash` read from wire as if per-path field, but nix wire encodes nar hash inside `ca` field — possible parsing mismatch
- **`base.py:373`** — `NarFromPathResponse.nar_data` is always `b""`; response type is misleading
- **`store.py:388-410`** — `nar_from_path_chunked` has untyped `write_chunk` parameter
- **`proxy.py:354-383`** — `QueryMissingResponse` always returns empty `will_substitute`/`unknown`/`download_size`/`nar_size`; pynixd never computes these

### Naming/Organization
- **`store.py:1`** — God module (see #7 above)
- **`scheduler.py:38`** — `BuildKey` is a plain tuple; sort lambdas use `x[0]`, `x[1].priority`, `x[2]` with no named fields
- **`scheduler.py:138-152`** — Tuple indices in sort lambdas throughout scheduling pass
- **`local_store_db.py:131,177`** — Inconsistent pluralization: `query_path_info()` vs `query_path_infos()`, `mark_path()` vs `mark_paths()`
- **`proxy.py:192`** — `_dispatch()` 77-line match handling 20+ operation types; should be split
- **`proxy.py:412`** — `_enrich_derivation()` mutates `request.derivation._is_dynamic` in-place after wire receipt

### Test Quality
- **`test_bench_build.py:124-165`** — Zero assertions (see #10 above)
- **`conftest.py:52-69`** — `get_current_system()` silently returns empty string on subprocess failure
- **`test_nar_transfer.py:85-268`** — 3 of 5 NAR transfer tests permanently skipped; `AddToStoreNar` streaming untested
- **`conftest.py:403-408`** — `nix_env` copies full host environment (`os.environ.copy()`) instead of minimal env
- **`test_matrix.py:38-44`** — Global `_counter` mutated by `_next_id()` without synchronization; breaks in pytest-xdist parallel execution
- **No unit tests for `scheduler.py`** — Critical scheduling logic only tested via full integration
- **No unit tests for `proxy.py`** — All client protocol dispatch only tested via full integration
