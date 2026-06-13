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

Worker ID in high bits of request ID (`wid << 48 | seq`).

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

**P2 — No timeout on `_WorkerRef.send_recv`**

The worker death path is partially handled: ``_read_responses`` sets
``_dead`` on pipe errors, and ``send_recv`` races ``dead_task`` against
``_responses.get()``.  Worker hangs are caught by the outer
``asyncio.timeout(t)`` (300s default).

**Actual deadlock:** ``req_conn.send()`` on line 107 runs via
``run_in_executor`` *before* the ``asyncio.timeout`` block.  If the
worker process is alive but not reading its input pipe (e.g. stuck in
a long Nix operation that doesn't drain the request pipe), the send
blocks the thread-pool thread forever.  The timeout is never reached.

Fix: move ``req_conn.send()`` inside the ``asyncio.timeout`` block so
a hung worker triggers ``TimeoutError`` instead of a permanent hang.
Also wrap ``close()``'s send in a timeout.

### Nix error signaling — logEI, STDERR_ERROR, result

Nix signals errors through three paths.  Two need work.

| Path | Mechanism | Status |
|------|-----------|--------|
| C++ exception | ``_worker.py`` ``except Exception`` → ``{"type":"error"}`` → ``RuntimeError`` | ✅ Works |
| ``STDERR_ERROR`` (daemon) | Nix daemon client converts to C++ exception → path above | ✅ Indirectly |
| ``logEI`` (ErrorInfo) | PyLogger emits ``("msg", lvlError, text)`` — indistinguishable from info | ❌ **Gap** |
| ``result`` callback | ``resultType`` carries ``resCorruptedPath`` etc. — logged but not acted on | ❌ **Gap** |
| Worker stderr | Goes to parent stderr unfiltered; Nix errors like ``error: …`` are never captured | ❌ **Gap** |

**logEI gap**: When Nix encounters evaluation or build errors, it calls
``Logger::logEI(ErrorInfo)``.  Our ``PyLogger`` converts this to a
``"msg"`` event with ``lvlError`` (0) — it looks like any other message.
The RPC call succeeds, and the caller never knows an error was logged.

Fix: emit ``"error"`` action instead of ``"msg"`` for ``logEI``, so the
Python side can distinguish errors from info messages.  Consider also
adding ``is_error`` / ``level`` to the ``LogEvent`` model.

**result gap**: The ``result`` callback carries ``nix::ResultType`` (e.g.
``resCorruptedPath = 103``).  These are passed through as log events but
never surfaced as exceptions.  At minimum, consumers should be able to
filter for ``resCorruptedPath`` / ``resUntrustedPath``.

**Worker stderr**: The subprocess writes tracebacks and Nix diagnostics
to stderr, which inherits the parent's fd.  Should be captured via a
pipe and relayed as ``stderr`` events in the log stream.

### 🟡 Design issues

**P3 — No backend Protocol or ABC**

``Store`` delegates to ``WorkerPool`` (the only backend).  There is no
``Protocol`` or ABC so alternative backends (in-process, mock) have no
contract to implement.  ``Store._imp`` is typed ``Any``.

Fix: define a ``StoreBackend`` Protocol that ``WorkerPool`` satisfies.
This also fixes P8.

**P4 — `EvalSession` pierces the `WorkerPool` abstraction**

Store calls go through `pool._send_recv()` (acquire→call→release).
Eval calls go through `pool._acquire()` / `pool._release()` (private
methods) + direct `worker.send_recv()`.  Two dispatch patterns.

Fix: add a proper `pool.reserve()` → `ReservedWorker` context manager that
both `_send_recv` (internally) and `EvalSession` use.

**P5 — `ValueProxy` holds a raw `_WorkerRef`**

```python
class ValueProxy:
    def __init__(self, worker: _WorkerRef, handle: int, typ: str): ...
```

If the worker dies between creating the proxy and calling `.force()`,
the proxy holds a dead reference.  `ValueProxy.release()` sends an RPC —
it could be called after `EvalSession.__aexit__` releases the worker.

Fix: tie ValueProxy lifetime to the session.  After `__aexit__`, all
proxies should be invalid.  Optionally, track proxies on the session
and prevent use after close.

**P6 — `LogEvent` model exists but is never used**  ✅ DONE

Fixed: ``Nix.log_stream()`` now maps wire-format ``id`` → ``request_id`` and
yields ``LogEvent.model_validate(...)`` instances.  ``LogEvent`` is exported
from ``__init__.py``.

### 🟢 Polish

**P7 — `EvalSession` and `ValueProxy` not re-exported from `__init__.py`**  ✅ DONE

Both are imported from ``_session`` and listed in ``__all__``.

**P8 — `Store._imp` typed as `Any`**

```python
@dataclass
class Store:
    _imp: Any  # WorkerPool
```

Loses all IDE completion.  Would benefit from a `Protocol` (same as P3).

**P9 — `_extract.py` inconsistencies**

- `store_path_str` splits on first `-` (fragile if Nix hash format changes).
- `locked_input` does inline `import nanopynix_flake` (circular-dep hack).
- Mix of positional-only (`/`) and regular params across functions.

**P10 — Background tasks not tracked or cancelled**

``WorkerPool._spawn()`` creates ``_read_responses`` and ``_relay_events``
via ``asyncio.ensure_future()`` but stores no ``Task`` handles.  On
``close()`` these continue running until the pipes close.

Fix: store task handles and cancel them on close.

**P11 — Response ID overflow**

`req_id = wid << 48 | seq` — the low 48 bits come from `_next_id`
which is unbounded.  After 2^48 calls to one worker, IDs wrap.

Fix: use a monotonic global counter with no bit-packing (the `wid` is
already known from which `_WorkerRef` you're talking to).

---

## Deferred

- **C++ boundary refactor** — return `nb::dict` instead of `nb::class_` for
  data types (StorePath, PathInfo, etc.). Extractor functions bridge for now.

## File layout

```
src/
    nanopynix/
        __init__.py    — L2 re-exports + L1 escape hatch
        _async.py      — AsyncStore, AsyncEvalState (in-process path)
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
    py_store.hh        — PyStoreHelper (unused?)
    py_store_impl.cpp  — register_python_store
    py_store_impl.hh   — header
tests/
    test_models.py     — 26 tests (no Nix dep)
    test_logging.py    — 6 tests (request_id tagging)
    test_store_l2.py   — 17 tests (Store facade over subprocess)
    test_workerpool.py — 5 tests (multi-worker concurrency)
    test_*.py          — existing L1 tests (import from nanopynix_* directly)
```
