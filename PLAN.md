# PLAN — nanopynix architecture

## Done

### L1 — C++ (unchanged)
Six `.so` modules compiled via CMake + scikit-build-core. Pydantic owns the
schema; L1 objects are converted to dicts at the boundary via `_extract.py`.

**New in L1:**
- `PyLogger` (in `nix_util.cpp`) — custom `nix::Logger` forwarding to Python
  callback with `request_id` tagging. All events: `(req_id, action, *args)`.
  Verbosity-checked before GIL acquire.
- `PyFlakeRef::to_attrs()` — attrs-based canonical representation.
- `PyEvalState` handle management — `export_value`/`get_exported`/
  `release_exported`/`release_all_exported`. GC-safe via explicit refcounting
  with `gc_allocator.h` (replicates `nix_gc_incref`/`nix_gc_decref` from
  libexpr-c without linking against it).

### L2 — Models, extractors, store facade
```
src/nanopynix/
    models.py     — Pydantic models (all attrs-based, no C++ deps)
    _extract.py   — L1 nanobind → dict converters
    _pool.py      — WorkerPool + _WorkerRef (spawn N, round-robin, log relay)
    _session.py   — EvalSession + ValueProxy (eval over RPC)
    store.py      — Store facade (returns Pydantic models)
    logging.py    — LogCollector (janus.Queue bridge, thread-safe)
    nix.py        — Nix manager (max_workers=...)
```

Key design decisions:
- All models are **attrs-based**: `Input(attrs=...)`, `FlakeRef(attrs=...)`,
  `LockedInput(attrs=...)`. URL↔attrs via `FlakeRef::fromAttrs`/`parseFlakeRef`.
- `Store` accepts `StorePath` model OR string.
- `LogEvent` has `request_id` for multiplexing.

### L3 — Multiprocess worker pool
```
src/nanopynix/
    _worker.py    — Subprocess RPC loop (stdin→dispatch→stdout)
    _pool.py      — _WorkerRef (single pipe reader, queue-per-direction)
                    WorkerPool (spawn N, round-robin, log relay)
    _session.py   — EvalSession (exclusive worker lock) + ValueProxy
    nix.py        — Nix(max_workers=4, store_uri=..., ...)
```

One subprocess = one `nix::Store` = one `nix::logger`. The pool dispatches
calls round-robin to free workers. Each worker has its own stdout reader
task that routes `{type:"result"}` to a response queue and `{type:"event"}`
to a log queue. No pipe conflicts.

Protocol:
```
→ {"type":"init", "store_uri":"daemon", "settings":{...}, "experimental_features":[...]}
← {"type":"ready"}
→ {"type":"call", "id":<int64>, "module":"store", "fn":"query_path_info", "args":["..."]}
← {"type":"event", "id":<int64>, "action":"msg", "args":[...]}
← {"type":"result", "id":<int64>, "value":{...}}
→ {"type":"close"}
```

Worker ID in high bits of request ID (``wid << 48 | seq``).  P11 fixed:
now uses ``itertools.count()`` — monotonic, no bit-packing, no wrap.

### GC-safe handle management

Boehm GC is live on NixOS. `Value*` in regular `std::map` (allocated by
`operator new`) is invisible to the conservative scanner. Solution:

```cpp
// Replicates nix_gc_incref/nix_gc_decref from the C API.
// traceable_allocator ensures map nodes are GC-visible.
static void _gc_incref(const void *p) {
    static RefCountMap &map = *new RefCountMap();
    map.insert_or_visit({p, 1}, [](auto &kv) { kv.second++; });
}
static void _gc_decref(const void *p) {
    static RefCountMap &map = *new RefCountMap();
    map.erase_if(p, [](auto &kv) { return !--kv.second; });
}
```

`export_value(v)` → `_gc_incref(v)`, `release_exported(h)` → `_gc_decref(v)`.

---

## Next: Phase 4 — EvalSession + ValueProxy  ✅ DONE

### Architecture

```
Worker (from pool)
  └── EvalSession (exclusive lock for duration)
        ├── EvalState
        ├── handle → Value* map (GC-safe)
        └── handles counter

Client side:
  EvalSession (acquired via nix.eval())
    └── ValueProxy(handle=1)
         ├── .force()    → RPC → force + to_python → plain value
         ├── .attr("n")  → RPC → attr_get → ValueProxy(handle=2)
         ├── .list_get(i)→ RPC → list_get → ValueProxy(handle=3)
         └── .release()  → RPC → release_exported
```

### API sketch

```python
async with Nix(max_workers=4) as nix:
    async with nix.eval() as session:
        root = await session.eval_file("/path/to/flake.nix")
        # → ValueProxy(handle=1)

        meta = await root.attr("meta")
        desc = await (await meta.attr("description")).force()
        # → plain Python string

        # Handles auto-released on __aexit__
```

### RPC additions

```
→ {"type":"call","id":...,"module":"eval","fn":"eval_file","args":[path]}
← {"type":"result","id":...,"value":{"handle":1,"type":"attrs"}}

→ {"type":"call","id":...,"module":"eval","fn":"attr","args":[handle,"name"]}
← {"type":"result","id":...,"value":{"handle":2,"type":"attrs"}}

→ {"type":"call","id":...,"module":"eval","fn":"force","args":[handle]}
← {"type":"result","id":...,"value":{...}}           # Python primitive

→ {"type":"call","id":...,"module":"eval","fn":"list_get","args":[handle,idx]}
← {"type":"result","id":...,"value":{"handle":3,"type":"string"}}

→ {"type":"call","id":...,"module":"eval","fn":"release","args":[handle]}
← {"type":"result","id":...,"value":null}

→ {"type":"call","id":...,"module":"eval","fn":"release_all","args":[[h1,h2]]}
← {"type":"result","id":...,"value":null}
```

### Worker dispatch (to add in `_worker.py`)

Implemented.  See `_worker.py` `_eval_dispatch()`.

---

## Production hardening — review items

### 🔴 Critical

**P1 — `asyncio.Queue` thread-safety bug in `logging.py`**  ✅ DONE

Fixed: ``LogCollector`` uses ``janus.Queue`` (sync_q for C++ callback from
any GIL thread, async_q for the event loop).  The ``asyncio.Queue``
instances in ``_pool.py`` are safe — accessed only from event-loop tasks.

**P2 — No timeout on `_WorkerRef.send_recv`**  ✅ DONE

Fixed: ``req_conn.send()`` moved inside the ``asyncio.timeout`` block.
Timeout is now an *idle* timeout — resets whenever the worker emits any
pipe message (log event, result, etc.).  Long builds survive because
Nix's log activity keeps the deadline fresh.  ``close()`` send also
has a 2s timeout.  Tests added: worker death (``WorkerDied``), idle
timeout (0.5s survives 3 calls).

### Nix error signaling — logEI, STDERR_ERROR, result

Nix signals errors through five paths.  Two are resolved; three remain.

| Path | Mechanism | Status |
|------|-----------|--------|
| C++ exception | ``_worker.py`` ``except Exception`` → ``{"type":"error"}`` → typed ``NixError`` subclass | ✅ Typed now |
| ``STDERR_ERROR`` (daemon) | Nix daemon client converts to C++ exception → path above | ✅ Indirectly |
| ``logEI`` (ErrorInfo) | PyLogger now emits ``("error", lvlError, text)`` — distinguishable | ✅ Fixed |
| ``result`` callback | ``resultType`` carries ``resCorruptedPath`` etc. — logged but not acted on | ✅ **Fixed** |
| Worker stderr | Goes to parent stderr unfiltered; Nix errors like ``error: …`` are never captured | ⬜ |

**logEI**: Changed from ``"msg"`` to ``"error"`` action in ``nix_util.cpp``.
Consumers can filter ``LogEvent.action == "error"``.  A future
``LogEvent.is_error`` computed field could also check ``action == "warn"``.

**C++ typed exceptions**: 12 Nix exception types are registered via
``nb::exception`` in ``nix_expr.cpp``, ``nix_store.cpp``, ``nix_util.cpp``.
Nanobind now preserves the C++ type name (e.g. ``"TypeError"``) as
``type(exc).__name__`` instead of ``"RuntimeError"``.  The ``_classify()``
function in ``exceptions.py`` additionally parses the error message for
redundant classification when the C++ type is not specific enough.

**result gap**: Fixed — ``ResultType`` IntEnum in ``models.py`` (values
100-108 matching ``nix::ResultType``).  ``Nix.log_stream()`` populates
``LogEvent.result_type`` when ``action == 'result'``, so consumers can
filter: ``if event.result_type == ResultType.corrupted_path: ...``.

**Worker stderr**: Not captured — Nix's "stderr" terminology refers to
``nix::Logger`` log events (which already flow through the RPC pipe as
``action:"msg"`` / ``action:"error"`` events).  Actual subprocess stderr
inherits the parent fd 2; worker↔master IPC uses only stdin/stdout.

### Structured error mapping — Nix → Python exceptions

**Nix error hierarchy** (simplified):

```
std::exception
 └─ BaseError             ← don't catch (includes Interrupted)
     ├─ Error              ← main base
     │   ├─ UsageError, UnimplementedError
     │   ├─ SystemError → SysError (errno) / WinError
     │   ├─ InvalidPath, Unsupported, SubstituteGone, BadStorePath, ...
     │   ├─ EvalBaseError
     │   │   ├─ EvalError
     │   │   │   ├─ AssertionError → ThrownError
     │   │   │   ├─ Abort, TypeError, UndefinedVarError
     │   │   │   ├─ MissingArgumentError, InfiniteRecursionError
     │   │   │   └─ InvalidPathError (carries StorePath)
     │   │   ├─ StackOverflowError, IFDError, RecoverableEvalError
     │   └─ ParseError, FormatError, BadURL, BadHash, ...
     └─ Interrupted        ← SIGINT
```

**ErrorInfo** — every Nix error carries:

```
level: int       # lvlError=0, lvlWarn=1, ..., lvlVomit=7
msg: str         # formatted message (without "error: " prefix)
status: int      # exit status (default 1)
traces: list     # stack traces with Pos + HintFmt
suggestions: list# "did you mean...?"
isFromExpr: bool # true for throw/abort/builtins.warn
```

**Current**: worker catches ``Exception`` → sends ``{"type":"error",
"msg":"TypeError: something"}`` → client raises ``RuntimeError("Worker
error: TypeError: something")``.  Error type, traces, suggestions are all
lost.

**Target**: structured error response → typed Python exceptions:

```
Worker error response:
  {"type":"error", "id":42,
   "error_type":"TypeError", "msg":"value is a string, not an integer",
   "info":{"level":0, "status":1, "is_from_expr":false,
           "traces":[{"hint":"while evaluating ..."}],
           "suggestions":["did you mean ...?"]}}

Client raises:
  nanopynix.exceptions.TypeError(msg, info=..., traces=...)
```

**Implementation status**:

**✅ Phase A — structured error response** (``_worker.py`` + ``_pool.py``)

- ``_worker.py`` error response now includes ``error_type``, ``msg``, and
  ``traceback`` fields instead of a flat ``msg`` string.
- ``_pool.py`` ``send_recv`` raises ``from_response(...)`` which constructs
  the right ``NixError`` subclass.
- ``logEI`` in ``nix_util.cpp`` changed from ``"msg"`` to ``"error"``
  action — errors in the log stream are now distinguishable.
- C++ exception types (``EvalError``, ``ParseError``, ``TypeError``,
  ``UndefinedVarError``, ``AssertionError``, ``ThrownError``,
  ``InvalidPath``, ``Unsupported``, ``BadStorePath``, ``SysError``,
  ``UsageError``, ``UnimplementedError``) are registered via
  ``nb::exception`` so nanobind preserves the C++ type name as
  ``type(exc).__name__`` instead of ``"RuntimeError"``.

**✅ Phase B — typed Python exceptions** (``exceptions.py``)

- 12-class hierarchy: ``NixError`` → ``StoreError``, ``EvalError``,
  ``ParseError``, ``UsageError``.  ``EvalError`` subclasses:
  ``TypeError_``, ``AssertionError_``, ``UndefinedVarError``,
  ``ThrownError``, ``InfiniteRecursionError``, ``RestrictedPathError``,
  ``MissingArgumentError``.
- ``_classify()`` uses 20 regex patterns (ordered most-specific-first) to
  parse the Nix error message and determine the right Python class.  Falls
  back gracefully to the C++ type name from the worker.
- ``from_response()`` factory called by ``_pool.py``.

**Remaining for full ErrorInfo extraction**: The ``ErrorInfo`` struct
fields (``level``, ``traces``, ``suggestions``) are not yet serialized.
``NixError.info`` is an optional ``dict`` field, currently always ``None``.
To populate it, the worker's RPC dispatch must catch ``nix::Error`` at the
C++ level (before nanobind converts it to a Python exception).  This requires
either a full C++ dispatch table or a nanobind-level interceptor.

### 🟡 Design issues

**P3 — No backend Protocol or ABC**  ⬜ Moot

``Store`` delegates to ``WorkerPool`` (the only backend).  There is no
``Protocol`` or ABC but one isn't needed — ``Store._pool`` is typed as
``WorkerPool`` directly (see P8).

**P4 — `EvalSession` pierces the `WorkerPool` abstraction**  ✅ DONE

Fixed: ``WorkerPool.reserve()`` → ``ReservedWorker`` (public API).
Both ``_send_recv`` (internally) and ``EvalSession`` use the same
``reserve() → send_recv → release()`` path.  ``_acquire``/``_release``
stay private — only called by ``reserve()`` and ``ReservedWorker.release()``.

**P5 — `ValueProxy` holds a raw `_WorkerRef`**  ✅ DONE

Fixed: ``EvalSession`` owns a shared ``_active: [bool]`` flag.  All
``ValueProxy`` instances receive a reference to it.  ``__aexit__`` sets
``_active[0] = False``; every RPC method checks ``_check_active()`` first.
Use-after-close raises ``RuntimeError``.  ``handle`` / ``type_name``
properties remain accessible (they're cached).

**P6 — `LogEvent` model exists but is never used**  ✅ DONE

Fixed: ``Nix.log_stream()`` now maps wire-format ``id`` → ``request_id`` and
yields ``LogEvent.model_validate(...)`` instances.  ``LogEvent`` is exported
from ``__init__.py``.

### 🟢 Polish

**P7 — `EvalSession` and `ValueProxy` not re-exported from `__init__.py`**  ✅ DONE

Both are imported from ``_session`` and listed in ``__all__``.

**P8 — `Store._imp` typed as `Any`**  ✅ DONE

Fixed: ``Store._pool: WorkerPool`` (renamed from ``_imp``).  No Protocol
needed — there is only one backend.

**P9 — `_extract.py` cleanup**  ✅ DONE

Fixed: inline ``import nanopynix_flake`` hoisted to top (no circular dep
— it's a C++ extension module).  ``store_path_str`` split-on-``-``
documented as reliable (Nix base32 hashes never contain ``-``).

**P10 — Background tasks not tracked or cancelled**  ✅ DONE

Fix: ``_read_responses`` puts ``None`` sentinel into ``_events`` on exit,
unblocking ``_relay_events``.  Task handles stored in ``_WorkerRef._read_task``
and ``WorkerPool._relay_tasks``.  Both ``close()`` methods await their tasks
after the worker process exits — no dangling tasks in asyncio strict mode.

**P11 — Response ID overflow**  ✅ DONE

``req_id`` now uses ``itertools.count()`` — monotonic, no bit-packing
(``wid`` was already known from which ``_WorkerRef``).  Removed
``req_id_base`` / ``_next_id`` from ``_WorkerRef`` completely.

---

## Deferred

- **C++ boundary refactor** — return `nb::dict` instead of `nb::class_` for
  data types (StorePath, PathInfo, etc.). Extractor functions bridge for now.

## File layout

```
src/
    nanopynix/
        __init__.py    — L2 re-exports + L1 escape hatch
        _extract.py    — L1→dict converters
        _pool.py       — WorkerPool + _WorkerRef
        _session.py    — EvalSession + ValueProxy
        _worker.py     — subprocess RPC loop
        models.py      — Pydantic models (StorePath, PathInfo, ...)
        store.py       — Store facade
        logging.py     — LogCollector (janus.Queue)
        nix.py         — Nix manager
    nix_expr.cpp       — PyValue, PyEvalState, handle mgmt, primop bridge
    nix_util.cpp       — PyLogger, settings
    nix_store.cpp      — StorePath, Store, PathInfo, BuildResult
    nix_fetchers.cpp   — Input
    nix_flake.cpp      — FlakeRef, LockedFlake (+ to_attrs)
    nix_main.cpp       — init_nix, init_plugins
    py_eval.hh         — PyEvalState struct
    py_store_impl.cpp  — register_python_store
    py_store_impl.hh   — header
tests/
    test_models.py     — 26 tests (no Nix dep)
    test_logging.py    — 6 tests (request_id tagging)
    test_store_l2.py   — 17 tests (Store facade over subprocess)
    test_workerpool.py — 5 tests (multi-worker concurrency)
    test_*.py          — existing L1 tests (import from nanopynix_* directly)
```
