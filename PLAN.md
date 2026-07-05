# PLAN — nanopynix architecture

## Current state (2026-07-05)

### L1 — C++ nanobind modules
Six `.so` modules compiled via CMake + scikit-build-core:
- `nanopynix_util` — PyLogger, settings, verbosity, experimental features
- `nanopynix_store` — StorePath, Store, PathInfo, BuildResult, MissingInfo, BuildMode, Python store trampoline
- `nanopynix_expr` — PyValue, PyEvalState, GC-safe handle management, primop bridge
- `nanopynix_fetchers` — Input (from_url, from_attrs, to_attrs)
- `nanopynix_flake` — FlakeRef, LockedFlake, parse/lock/get
- `nanopynix_main` — init_nix, init_plugins

Key C++ features:
- GC-safe Value* handle management via `traceable_allocator` + `_gc_incref`/`_gc_decref`
- `PyLogger` — custom `nix::Logger` forwarding to Python callback with `request_id` tagging
- 12 Nix exception types registered via `nb::exception` for type preservation
- Primop bridge: Python functions exposed as Nix builtins, with error detail preserved
- `attrs_to_nb_dict()` shared header (deduplicates `std::visit` in fetchers + flake)
- `PyStoreImpl` trampoline: delegates `nix::Store` virtual methods to Python, with error logging

### L2 — Python package (`src/nanopynix/`)
```
__init__.py   — L2 re-exports + L1 escape hatch
models.py     — Pydantic models: StorePath, PathInfo, BuildResult, MissingInfo,
                Input, FlakeRef, LockedInput, LockedFlake, LogEvent, ResultType
_exceptions.py — 12-class NixError hierarchy, 22-pattern _classify() engine
_extract.py   — L1 nanobind → dict converters (with error guards)
_pool.py      — WorkerPool + _WorkerRef + ReservedWorker
                (concurrent worker close, _log_done Event, idle timeout,
                 spawn cleanup on failure, generic call() method)
_session.py   — EvalSession + ValueProxy
                (try/finally worker release, _check_rw guard, _active flag)
_worker.py    — Subprocess RPC loop (_reset_es per session)
store.py      — Store facade (Pydantic models, BuildMode enum, str/StorePath coercion)
logging.py    — LogCollector (janus.Queue bridge, sync drain + async stream)
nix.py        — Nix manager (60s close timeout, None-sentinel filtering)
```

### L3 — Multiprocess worker pool
- Forkserver-based subprocess workers (`multiprocessing.Pipe`)
- Protocol: `init` → `ready` → `call`/`result`/`event` → `close`
- `WorkerPool.call(module, fn, args)` — generic RPC entry point
- `ReservedWorker` — exclusive worker lease for `EvalSession`
- `Nix.log_stream()` — `LogEvent` stream with `result_type` mapping
- Concurrent worker shutdown via `asyncio.gather`

### Test suite (293 tests, 1 skipped)
```
tests/
    conftest.py              — fixtures (store, eval_state, primops, flakes)
    test_models.py           — 33 tests (Pydantic models including ResultType, edge cases)
    test_logging.py          — 8 tests (LogCollector async stream + sync drain, verbosity, req_id)
    test_exceptions_classify.py — 44 tests (every regex pattern, ANSI stripping, fallback, factory, ordering)
    test_extract.py          — 19 tests (store_path_str, store_path, path_info, missing_info,
                                input_attrs, flake_ref_attrs, locked_input, locked_flake)
    test_session_unit.py     — 22 tests (EvalSession lifecycle, ValueProxy, ReservedWorker — mock-based)
    test_store_unit.py       — 18 tests (Store coercion logic — mock-based)
    test_store_l2.py         — 17 tests (Store facade over subprocess)
    test_workerpool.py       — 7 tests (multi-worker concurrency, error propagation, worker death)
    test_eval_rpc.py         — ~12 tests (eval over RPC)
    test_store.py            — L1 store tests
    test_expr.py             — L1 eval tests
    test_fetchers.py         — L1 fetchers tests
    test_flake.py            — L1 flake tests
    test_primops.py          — primop bridge tests
    test_util.py             — L1 util tests
    test_main.py             — L1 main tests
    test_store_impl.py       — Python store impl tests
```

---

## Production hardening — review items status

### Critical: all resolved ✅
- P1: asyncio.Queue thread-safety → `janus.Queue` ✅
- P2: No timeout on send_recv → idle timeout with activity reset ✅

### Design issues: all resolved ✅
- P3: No backend Protocol → moot (single backend) ✅
- P4: EvalSession pierces WorkerPool → `reserve()`/`ReservedWorker` ✅
- P5: ValueProxy holds raw _WorkerRef → `_active` flag ✅
- P6: LogEvent unused → `Nix.log_stream()` yields models ✅

### Polish: all resolved ✅
- P7: EvalSession/ValueProxy re-exported ✅
- P8: Store._imp typed as Any → `_pool: WorkerPool` ✅
- P9: _extract.py cleanup ✅
- P10: Background tasks tracked ✅
- P11: Response ID overflow → `itertools.count()` ✅

### 2026-07-04 architecture audit — all resolved ✅
- Nix.log_stream() None-sentinel crash → filtered
- _es singleton leak → `_reset_es()` per session
- EvalSession.__aexit__ worker leak → try/finally
- Primop bridge swallowed Python traceback → `PyErr_Fetch`
- _acquire list mutation during close → concurrent gather
- _spawn FD/zombie leak on init failure → cleanup
- store_path_str crash on malformed input → ValueError
- MissingArgumentError/invalid_value ordering bugs → reordered
- locked_flake method-as-property bug → fixed
- py_store_impl.cpp silent catch → `fprintf(stderr, ...)`
- Store.build_derivation int→BuildMode → enum accepted
- locked_input parse_flake_ref crash → try/except guard

---
## 2026-07-05 code smell scan — architectural notes

### 🔴 Architecture — needs design discussion before fixing

**A1. `evalRef()` shared_ptr with no-op deleter** (`src/py_eval.hh:21-26`)
Creates `shared_ptr<PyEvalState>` that aliases another object's lifetime. If original
is destroyed first, `PyValue.evalState()` returns dangling reference. `noop_deleter`
silences sanitizers. Fix: actual `shared_from_this` or store `ref<EvalState>`
separately from `PyEvalState`.

**A2. WorkerPool uses default ThreadPoolExecutor** (`src/nanopynix/_pool.py:76-77`)
`loop.run_in_executor(None, self._resp_conn.recv)` uses global default pool. 4 workers
= 4 permanent thread-blockers. `send` calls also use same pool — under load the
pool saturates and **deadlocks the event loop**. Fix: dedicate `ThreadPoolExecutor`
per WorkerPool.

**A3. String regex error classification** (`src/nanopynix/exceptions.py:96-129`)
Classifies Nix errors by parsing human-readable error strings. These are not a
stable API — they change across Nix/Lix versions. 20 overlapping patterns with
order-dependent matching. Fix: push structured error enums to C++ boundary, or
version-negotiate regex tables, or log warnings on unclassified errors.

**A4. Duplicate Nix→Python conversion** (`src/nix_expr.cpp:244-290,305-338`)
Two near-identical `to_python`/`value_to_python_arg` functions. Both do shallow
`*v = *attr.value` copy (unsound for GC-managed Values). One converts nested attrs
differently. Fix: single parameterized converter with deep-copy semantics.

**A5. 70-line send_recv with manual timeout polling** (`src/nanopynix/_pool.py:94-163`)
Hand-rolled poll loop with `asyncio.ensure_future` in hot loop, fire-and-forget
`task.cancel()`, and race window between timeout expiry and `_last_activity` check.
Fix: replace with `asyncio.wait_for` over combined future + watchdog task.

### 🟡 Medium — concrete fixes, no design required

**B1. StorePath→str coercion duplicated 11x** — extract `_to_store_path_str()` helper
**B2. `_try_send` silently discards log events** — use bounded buffer with backpressure
**B3. Duplicated `_to_dict` in `_worker.py` and `_extract.py`** — unify
**B4. `__aexit__` swallows `release_all` errors** — log before finally
**B5. `read_derivation` returns raw dict** — define Derivation model
**B6. `next_id` instance method on module counter** — make `@staticmethod`
**B7. Redundant `@property` on `is_derivation`** — remove
**B8. Duplicated default `"<string>"` path** — extract constant
**B9. `import os` inside 10+ test methods** — move to module level
**B10. Dead test `test_query_derivation_outputs`** — implement or delete
**B11. 10x repeated bash StorePath fixture** — extract `@pytest.fixture`
**B12. `add_temp_root` GC root leak in tests** — remove root after test
**B13. `mkdtemp()` leaked temp dirs** — use `tmp_path` fixture
**B14. Stderr print for close timeout** — use `logging.warning`
**B15. Unbounded janus.Queue** — set maxsize default
**B16. QueueShutDown exception is Python 3.13-only** — verify/fix

### 🟢 Deferred from previous audits (still open)

- Full ErrorInfo serialization (traces, suggestions from `nix::ErrorInfo`)
- GC bindings (collectGarbage, deletePath)
- `nix.conf` path support in Nix/WorkerPool
- Fetchers/flake C++ wrappers → dicts (same pattern as store refactor done)

---
## Current state (2026-07-05)
