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
exceptions.py — 12-class NixError hierarchy, 22-pattern _classify() engine
_extract.py   — L1 nanobind → dict converters (with error guards)
_pool.py      — _WorkerManager + ReservedWorker
                (single asyncio subprocess, stdin/stdout JSON-RPC,
                 idle timeout, log bus, generic call() method)
_session.py   — EvalSession + ValueProxy
                (try/finally worker release, _check_rw guard, _active flag)
_worker.py    — Subprocess RPC loop (_reset_es per session)
store.py      — Store facade (Pydantic models, BuildMode enum, str/StorePath coercion)
logging.py    — LogCollector (janus.Queue bridge, sync drain + async stream)
nix.py        — Nix manager (60s close timeout, None-sentinel filtering)
```

### L3 — Single subprocess RPC runtime
- One worker subprocess per `Session`, started with `python -m nanopynix._worker`
- Transport: newline-delimited JSON-RPC over stdin/stdout
- Actual OS stderr is only read for diagnostics; Nix log events travel over JSON-RPC
- `_WorkerManager.call(module, fn, args)` — generic RPC entry point guarded by one lock
- `ReservedWorker` — exclusive worker lease for `EvalSession`
- `Session.log_stream()` — `LogEvent` stream with `result_type` mapping
- Shutdown: JSON-RPC shutdown notification, stdin close, bounded subprocess wait

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
    test_workerpool.py       — 7 tests (single-worker concurrency, error propagation, worker death)
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
- P4: EvalSession pierces worker manager → `reserve()`/`ReservedWorker` ✅
- P5: ValueProxy holds raw worker ref → `_active` flag ✅
- P6: LogEvent unused → `Nix.log_stream()` yields models ✅

### Polish: all resolved ✅
- P7: EvalSession/ValueProxy re-exported ✅
- P8: Store._imp typed as Any → `_pool: _WorkerManager` ✅
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

**A2. Obsolete after JSON-RPC refactor** ✅
The old `WorkerPool` / `run_in_executor(None, self._resp_conn.recv)` design no
longer exists. Current `_WorkerManager` uses `asyncio.create_subprocess_exec`
with `StreamReader` / `StreamWriter`, so do not implement the old dedicated
`ThreadPoolExecutor` fix.

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

**B1. StorePath→str coercion duplicated 11x** ✅ — `_to_str()`/`_to_strs()` helpers
**B2. `_try_send` silently discards log events** — deferred (needs design discussion)
**B3. Duplicated `_to_dict` in `_worker.py` and `_extract.py`** ✅ — import from _extract
**B4. `__aexit__` swallows `release_all` errors** ✅ — logging.warning
**B5. `read_derivation` returns raw dict** — deferred (needs Derivation model, sizable)
**B6. `next_id` instance method on module counter** ✅ — @staticmethod
**B7. Redundant `@property` on `is_derivation`** ✅ — removed
**B8. Duplicated default `"<string>"` path** ✅ — _DEFAULT_EVAL_PATH constant
**B9. `import os` inside 10+ test methods** ✅ — module level
**B10. Dead test `test_query_derivation_outputs`** ✅ — pytest.skip with reason
**B11. 10x repeated bash StorePath fixture** ✅ — _bash_sp() helper
**B12. `add_temp_root` GC root leak in tests** — skipped (no remove_temp_root in API, bash is permanent)
**B13. `mkdtemp()` leaked temp dirs** ✅ — tmp_path fixture
**B14. Stderr print for close timeout** ✅ — logging.warning
**B15. Unbounded janus.Queue** ✅ — maxsize=10_000
**B16. QueueShutDown exception is Python 3.13-only** — non-issue (project requires ≥3.13)

Also completed beyond the scan list:
- **send_recv race window** ✅ — get_nowait() check before TimeoutError
- **`_acquire` stalls on close** ✅ — background task for stale worker close

### 🟢 Deferred from previous audits (still open)

- Full ErrorInfo serialization (traces, suggestions from `nix::ErrorInfo`)
- GC bindings (collectGarbage, deletePath)
- `nix.conf` path support in Session/_WorkerManager
- Fetchers/flake C++ wrappers → dicts (same pattern as store refactor done)

---
## 2026-07-06 architecture + Python best-practices audit

This section is written for a workhorse AI. Keep changes small, add tests for
each behavior, and follow `AGENTS.md` / `REASONIX.md` test-output discipline:
preserve full pytest output with `tee` before using `tail`, `grep`, or `head`.

### Architecture correction

The current runtime is **not** a multiprocess worker pool. It is a single
asyncio subprocess manager:

- Parent side: `_WorkerManager` in `src/nanopynix/_pool.py`
- Worker side: `python -m nanopynix._worker`
- Transport: newline-delimited JSON-RPC over subprocess stdin/stdout
- Concurrency: one in-flight RPC at a time behind `_available`
- Eval sessions: reserve the single worker exclusively until `release_all`

Future plan items should avoid resurrecting pool-specific terminology unless a
real multi-worker design is intentionally added.

### Static-check baseline from this audit

Commands run on 2026-07-06:

- `direnv exec . pyright` → 56 errors, 1 warning
- `direnv exec . ruff check src/nanopynix` → 77 findings

Do not paper over these by loosening tool config. Fix the public API and typing
issues first, then handle pure style/lint cleanup.

### 🔴 P0 — correctness issues to fix first

**C1. `Session.log_stream()` uses the wrong request-id key**

- Evidence: worker emits `{"request_id": req_id, ...}` in
  `src/nanopynix/_worker.py:64-67`, but `Session.log_stream()` reads
  `raw["id"]` in `src/nanopynix/nix.py:105-109`.
- Impact: any real worker log event can raise `KeyError` before becoming a
  `LogEvent`.
- Workhorse task:
  - Change `Session.log_stream()` to accept `request_id`.
  - Optionally tolerate legacy `id` with `raw.get("request_id", raw.get("id", 0))`
    if compatibility is useful.
  - Update the stale model docstring that says wire format uses `id`.
  - Add a unit test with a fake manager yielding `{"request_id": 42, "action":
    "msg", "args": [...]}` and assert a valid `LogEvent`.
  - Add or strengthen an integration test that actually collects at least one
    worker log event; `tests/test_workerpool.py::test_concurrent_log_stream`
    currently does not assert anything about collected events.
- Validate:
  - `timeout 180 direnv exec . pytest tests/test_workerpool.py tests/test_logging.py 2>&1 | tee /tmp/pytest-logstream.log`
  - `direnv exec . pyright`

**C2. `ValueAttrs.__getitem__` and `ValueList.__getitem__` return proxies that
cannot identify the requested child**

- Evidence: `ValueAttrs.__getitem__` returns `ValueProxy(self._worker,
  self._handle, "thunk", ...)` without storing the attribute name
  (`src/nanopynix/_session.py:187-190`). `ValueList.__getitem__` does the same
  without storing the index (`src/nanopynix/_session.py:248-250`).
- Impact: `attrs["name"].force()` and `lst[0].force()` force the parent handle,
  not the selected child. The working methods are `ValueAttrs.force(name)` and
  `ValueList.force(idx)`, which means the documented lazy indexing API is
  misleading.
- Workhorse task:
  - Choose one simple fix:
    - remove `__getitem__` until a real lazy child proxy exists, or
    - introduce a small child proxy that stores `(parent_handle, selector)` and
      resolves via `attr` / `list_get` on first force.
  - Prefer the child-proxy fix if preserving the documented API matters.
  - Add tests for `attrs["x"].force()` and `lst[0].force()` that verify the RPC
    call uses `attr` / `list_get`, not `force` on the parent.
- Validate:
  - `timeout 180 direnv exec . pytest tests/test_session_unit.py tests/test_eval_rpc.py 2>&1 | tee /tmp/pytest-lazy-index.log`

**C3. Public `nanopynix.Store` export is listed but not bound**

- Evidence: `src/nanopynix/__init__.py:93` includes `"Store"` in `__all__`, but
  only `StoreHandle` is imported from `nanopynix.store` at
  `src/nanopynix/__init__.py:37`. `pyright` reports this.
- Impact: `from nanopynix import *` exposes a broken API contract; direct
  `nanopynix.Store` access is not guaranteed.
- Workhorse task:
  - Import `Store` from `nanopynix.store` or remove it from `__all__`.
  - Recommendation: import `Store` as the backward-compatible L2 alias and leave
    the L1 store available only as the private `_L1Store` unless intentionally
    exposed under a distinct name.
  - Add a small test that `nanopynix.Store is nanopynix.StoreHandle`.
- Validate:
  - `direnv exec . pyright`
  - `direnv exec . ruff check src/nanopynix/__init__.py tests`

**C4. `capture=True` does not capture logs despite the model contract**

- Evidence: `Capture` promises `logs=[LogEvent, ...]` in
  `src/nanopynix/models.py:141-149`, but store/eval methods return
  `Capture(result)` without wiring per-call log collection. There is no
  request-scoped log capture around `_WorkerManager.call()` / `_send_recv()`.
- Impact: the public API advertises observability that does not exist; callers
  may silently miss build/eval logs.
- Workhorse task:
  - Either implement request-scoped capture or remove/rename `capture=True`.
  - Recommended small step: make `capture=True` honest by capturing worker log
    events whose `request_id` matches the RPC id. This likely needs `_send_recv`
    to return both result and collected events for capture paths, while keeping
    normal calls unchanged.
  - If implementation is too large, first deprecate/remove the flag and update
    docs/tests so no API promises log capture.
- Validate:
  - focused unit tests for captured log filtering by request id
  - `timeout 180 direnv exec . pytest tests/test_store_unit.py tests/test_session_unit.py tests/test_logging.py 2>&1 | tee /tmp/pytest-capture.log`

### 🟠 P1 — type safety and API hygiene

**D1. Fix pyright before broad refactors**

- Current pyright clusters:
  - optional member access in `_pool.py` and `_session.py`
  - possibly-unbound `msg` in `_pool._send_recv`
  - `_WorkerManager.call()` typed as `dict` even though RPC results may be
    `str`, `bool`, `int`, `list`, or `None`
  - `StoreHandle` methods expose `Capture[T] | T`, which confuses callers when
    `capture` is a literal false default
  - nanobind-generated private helpers such as `_export_pyvalue`,
    `_cleanup_primop_registry`, and `_log_test` have no stubs
- Workhorse task:
  - Introduce a small `JsonValue` / `RpcResult` type alias for `_send_recv()` and
    `call()` instead of pretending all results are `dict`.
  - Add `@overload` signatures for `capture: Literal[False]` and
    `capture: Literal[True]` on public store/eval methods that currently return
    `Capture[T] | T`.
  - Narrow `self._rw` after `_check_rw()` with a local variable or a helper that
    returns `ReservedWorker`.
  - Add `.pyi` stubs or local `TYPE_CHECKING` protocols for nanobind-only helper
    methods used by tests/workers.
- Validate:
  - `direnv exec . pyright`

**D2. Pydantic models use mutable class defaults**

- Evidence: `DerivationOutputs.outputs`, `DerivationOutputs.dynamic_outputs`,
  `Derivation.args`, `Derivation.env`, `Derivation.input_drvs`, and
  `Derivation.input_srcs` use `=[]` / `={}` defaults in
  `src/nanopynix/models.py:154-168`.
- Impact: Pydantic usually copies defaults, but `default_factory` is clearer,
  safer, and consistent with the rest of the model layer.
- Workhorse task:
  - Replace mutable defaults with `Field(default_factory=list)` or
    `Field(default_factory=dict)`.
  - Add a regression test that two model instances do not share defaults.
- Validate:
  - `timeout 180 direnv exec . pytest tests/test_models.py 2>&1 | tee /tmp/pytest-models.log`

**D3. `read_derivation()` returns raw dicts while a `Derivation` model exists**

- Evidence: `StoreHandle.read_derivation()` returns `dict` in
  `src/nanopynix/store.py:200-207`; `Derivation` and `DerivationOutputs` exist
  in `src/nanopynix/models.py:154-168`, but the C++ dict keys are currently
  `platform`, `inputSrcs`, `inputDrvs`, etc. (`src/nix_store.cpp:320-342`), which
  do not match the model names.
- Impact: derivation data is an untyped API island and model drift can continue.
- Workhorse task:
  - Decide whether to change the C++ output keys to Python model names or add
    Pydantic aliases.
  - Make `StoreHandle.read_derivation()` validate and return `Derivation`.
  - Add tests for one known derivation fixture.
- Validate:
  - `timeout 180 direnv exec . pytest tests/test_store_unit.py tests/test_store_l2.py 2>&1 | tee /tmp/pytest-derivation.log`

**D4. Clean up package exports deliberately**

- Evidence: `ruff` flags unused private L1 imports in `__init__.py` and unsorted
  `__all__`; `pyright` flags the missing `Store`.
- Workhorse task:
  - Decide whether L1 classes should be public under names like `L1Store`,
    `L1Input`, `L1FlakeRef`, or remain private implementation imports.
  - Do not expose two different concepts under the same name.
  - Sort `__all__` only after the API decision is made.
- Validate:
  - `direnv exec . ruff check src/nanopynix/__init__.py`
  - `direnv exec . pyright`

### 🟡 P2 — C++ boundary hardening

**E1. Replace `PyEvalState::evalRef()` no-op `shared_ptr`**

- Evidence: `src/py_eval.hh:58-60` creates `std::shared_ptr<PyEvalState>(this,
  noop_deleter)`.
- Impact: `PyValue` can hold an alias that does not own the `PyEvalState`.
  Destruction order bugs become dangling `EvalState` access and are hard for
  sanitizers to catch.
- Workhorse task:
  - Convert `PyEvalState` to use `std::enable_shared_from_this<PyEvalState>` if
    all construction paths can guarantee shared ownership, or redesign `PyValue`
    to store a safe owner/reference that matches nanobind lifetime semantics.
  - Add a regression test that a `Value` cannot outlive its `EvalState` and crash
    on `force()` / `to_python()`.
- Validate:
  - `timeout 180 direnv exec . pytest tests/test_expr.py tests/test_eval_rpc.py 2>&1 | tee /tmp/pytest-eval-lifetime.log`

**E2. Deduplicate Nix `Value` → Python conversion**

- Evidence: `PyValue::to_python()` and `value_to_python_arg()` in
  `src/nix_expr.cpp` independently convert Nix values and differ in allocation /
  copying behavior.
- Impact: primop arguments and public `to_python()` can diverge for nested attrs,
  paths, and future types; the shallow `*v = *attr.value` copies are especially
  suspect around GC-managed values.
- Workhorse task:
  - Create one internal conversion helper parameterized by ownership/copy policy.
  - Add tests that primop arguments and `Value.to_python()` produce the same
    nested Python structure for attrs, lists, paths, bools, ints, strings, nulls.
- Validate:
  - `timeout 180 direnv exec . pytest tests/test_expr.py tests/test_primops.py 2>&1 | tee /tmp/pytest-value-conversion.log`

### 🟢 P3 — lint cleanup after behavior is fixed

Only do this after P0/P1 items, because some ruff findings are style around code
that should be changed anyway.

- Remove unused imports (`uuid`, `Derivation`, `Any`, private L1 aliases if not
  exported).
- Move imports in `models.py` to the top.
- Replace Python 3.13-only generic `Capture(Generic[T])` with the project’s
  preferred style; note that `pyproject.toml` says `requires-python >=3.10` while
  ruff/pyright target 3.13. Decide which support policy is real before using
  PEP 695 syntax.
- Suppress or intentionally ignore noisy lint rules only when they conflict with
  API choices (`WorkerDied`, `TypeError_`, `AssertionError_`, `timeout` keyword
  parameters).

Validation target for this phase:

- `direnv exec . ruff check src/nanopynix`
- `direnv exec . pyright`
- `timeout 180 direnv exec . pytest tests 2>&1 | tee /tmp/pytest-full.log`
