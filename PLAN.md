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

### Deferred
- C++ boundary refactor — return `nb::dict` instead of `nb::class_`
- Full ErrorInfo serialization (traces, suggestions from `nix::ErrorInfo`)
- GC bindings (collectGarbage, deletePath)
- `nix.conf` path support in Nix/WorkerPool
