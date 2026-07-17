# Multithreaded L2 implementation handoff

## Goal

Make `nanopynix.inproc.Session` the high-performance in-process Nix API.
The target is real parallelism in one Python process:

- multiple independent, thread-confined `EvalState`s;
- concurrent Store query and build work through a shared logical Store;
- IFD preserved and routed through a bounded build path;
- explicit builds and IFD sharing one build-concurrency limit;
- operation-scoped log attribution across concurrent Nix threads;
- correct evaluator-thread lifetime under Boehm GC.

This supersedes the current single-executor L2 architecture. During this work,
run only dedicated multithreading tests; defer linting, type checking, the
broad suite, documentation cleanup, and Nix-version compatibility checks.

## Established Nix constraints

### Thread ownership

- `Store` is thread-safe and may be shared across threads.
- An `EvalState`, its `Value`s, REPL state, and locks are thread-confined. All
  creation, use, release, and destruction happen on that evaluator's dedicated
  OS thread.
- Store creation thread is irrelevant. A Store may be opened on one thread and
  retained/used from evaluator, query, and build threads.
- Every Python-created evaluator thread must register with the same Boehm GC
  instance used by Nix before its first evaluator allocation, then unregister
  immediately before that dedicated thread exits.

### Eval store versus build store

Nix `EvalState` has two Store references:

```text
EvalState.store       -> derivation writes, fetchers, path coercion, normal eval support
EvalState.buildStore  -> IFD and Nix string-context realisation
```

- `buildStore` is selected at `EvalState` construction.
- IFD blocks the evaluator thread which triggered it. Python scheduling cannot
  make that evaluator proceed before the required result exists.
- Explicit `buildPathsWithResults` chooses target Store and `evalStore` per
  call.
- If target and eval Store are the same canonical object, omit a separate
  `evalStore` to avoid Nix copy-closure work.
- For genuinely distinct physical Stores, pass the originating eval Store and
  let Nix perform its supported closure/copy handling.

### Remote and local Store behavior

- A `RemoteStore` has a per-instance connection pool controlled by
  `max-connections`; long builds pin a connection for their full duration.
- Thread-safe does not mean non-blocking: a cache-miss query on the same
  remote Store may wait when all connections are held by builds.
- One canonical shared RemoteStore avoids pointer-identity copy overhead.
  Query/build isolation comes from external admission control and a sufficient
  canonical connection pool, not from reopening the same URI repeatedly.
- A LocalStore should remain one shared object. Concurrent calls already create
  independent Nix Workers/build children; reopening it adds overhead and cold
  caches without useful build concurrency.

## Public L2 API

`inproc.Session` should expose:

```python
inproc.Session(
    ...,
    query_workers: int | None = None,
    build_workers: int | None = None,
    max_evaluators: int | None = None,
)
```

- `query_workers` bounds Store query/mutation jobs.
- `build_workers` bounds aggregate build/realisation work.
- `max_evaluators=None` is explicitly uncapped; callers creating many
  evaluators own CPU, memory, and connection pressure. A finite cap makes
  `EvalSession.open()` await a slot.
- Defaults are CPU-aware positive values.
- Remote Store `max-connections` derives from query, build, and evaluator
  metadata headroom. With an uncapped evaluator count, no finite connection
  budget guarantees evaluator metadata will never contend; document this.
- Preserve IFD by default.

Extend evaluation selection:

```python
session.eval(store, *, build_store: Store | None = None)
```

`build_store=None` means use `store`. A real cross-store build Store remains
supported for remote IFD/build routing.

Expose `Value.get_derived_path()` as the evaluator-side boundary. It returns
the canonical DerivedPath string, which is self-contained and can cross Python
threads safely. Store owns every build entry point:

```python
paths = await asyncio.gather(*(value.get_derived_path() for value in values))
await store.build_paths_with_results(paths)
```

A plain `.drv` string means all outputs; `^` opts into explicit canonical
DerivedPath output selection. Callers may use `asyncio.gather()` for separate
Store build requests, or pass a batch to one request for Nix `max-jobs`
scheduling. Do not introduce a `PreparedBuild` or an evaluator-owned batch
build API.

Remove or internalize generic `Store.call(method, ...)`: it bypasses operation
classification and can route builds through query scheduling. Compatibility is
not required.

## Canonical Store and build gate

Each public logical Store owns:

```text
canonical raw Store
BuildGateStore wrapper -> delegates to canonical Store
```

For the ordinary same-Store case, pass the wrapper as both `EvalState.store`
and `EvalState.buildStore`. The wrapper unwraps itself to canonical Store when
delegating an `evalStore` argument. Nix therefore sees canonical pointer
identity inside `copyDrvsFromEvalStore` and avoids redundant copy closures.

`BuildGateStore` is a binding-owned C++ Store forwarding decorator. It must:

- retain canonical Store and a shared native build gate;
- forward the Store virtual surface to canonical Store;
- acquire the gate around `buildPaths`, `buildPathsWithResults`,
  `buildDerivation`, `ensurePath`, and `repairPath`;
- call the corresponding method on canonical Store after acquisition;
- avoid Nix base-class default build implementations, which can incorrectly
  construct a Worker against the wrapper instead of a wrapped RemoteStore;
- preserve capability/dynamic behavior by delegating to canonical methods.

The gate is authoritative because both explicit `Value.build()` and implicit
IFD reach it. `ensurePath` matters because impure `builtins.storePath` can
trigger realisation via `EvalState.store`, bypassing `buildStore` alone.

## C++ binding work

### EvalState build Store

Update `bindings/src/py_eval.hh`:

- retain normal and optional build Store pointers;
- accept `build_store` in the Python/nanobind constructor;
- pass it to Nix `EvalState(lookupPath, store, fetchSettings, evalSettings,
  buildStore)`;
- thread it through `LocalRuntime.open_eval_state`, `LocalEvalState`,
  `inproc.EvalSession.open`, and `Session.eval`.

### Boehm bridge

Add narrow binding-owned internal functions:

```text
enter_evaluator_thread()
exit_evaluator_thread()
```

- Link/include the same `bdw-gc` instance used by Nix libexpr; never introduce
  a second collector.
- Register before `EvalState`/`Value` activity and unregister after all
  evaluator-local objects are destroyed.
- Keep raw Boehm calls out of Python user APIs.
- Add needed CMake/pkg-config configuration and later validate against every
  supported Nix version.

### Thread-local logger context

`PyLogger` currently uses one shared `_req_id`, which races across threads.

- Replace it with C++ `thread_local` operation context.
- Expose push/set and restore operations for executor wrappers.
- Every native Store/evaluator submission receives a monotonic Session
  operation ID and restores the prior context afterward.
- Preserve Nix `ActivityId`/parent IDs as the nested Nix hierarchy.
- Make `LogCollector` accounting thread-safe: Janus queue operations are safe,
  but its `_enqueued += 1` counter currently races.

## Python execution architecture

### Evaluator lane

Replace generic `ThreadPoolExecutor(max_workers=1)` use for EvalStates with a
dedicated evaluator lane:

```text
one long-lived OS thread per open EvalState
  -> Boehm register
  -> create EvalState
  -> serial queue of Value/EvalState work
  -> release Values, locks, REPL state, EvalState
  -> Boehm unregister
  -> thread exits
```

- Create it only after evaluator-capacity admission.
- All Value, child Value, REPL, lock, and release work routes to its owner.
- Reopening a closed EvalSession creates a new lane and EvalState.
- Store pool threads never touch EvalState/Value and need no Boehm registration.

### Query and build scheduling

Session owns bounded domains:

```text
query executor
  -> complete Store query closures on canonical Store

build executor
  -> DerivedPath-string execution through BuildGateStore
```

Every Store public method becomes one native submission: parsing, Nix call,
and conversion all run together. Do not transfer raw `StorePath` or other C++
intermediates between pool tasks.

The native build gate, rather than build executor worker count, is the
aggregate build limit because IFD enters via evaluator lanes.

### Explicit build handoff

1. On Value's evaluator lane, use `getDerivation` to produce the canonical
   DerivedPath string.
2. Submit that string through a Store build API and BuildGateStore. Store
   normalizes a plain `.drv` to `DerivedPath::Built` with all outputs.
3. Same canonical Store: build with no separate `evalStore`; true cross-store:
   pass source eval Store.

## Lifecycle and cancellation

Normal Session close:

1. Mark closing and reject new query/build/evaluator submissions.
2. Stop evaluator admission.
3. Drain Store work, evaluator work, and build-gate waiters.
4. On owner threads, release rooted Values, flake locks, REPL resources, and
   EvalStates.
5. Unregister and join evaluator threads.
6. Release wrappers and canonical Stores.
7. Shut down query/build executors.
8. Remove process-global logger after all Nix work is silent.
9. Release process-global Session guard.

Cancellation rules:

- queued work can be cancelled and releases admission;
- a running Nix operation remains tracked even if its Python awaiter is
  cancelled;
- native resources are never destroyed while running work can reference them;
- `wait=False` refuses close with outstanding work;
- timeout leaves resources alive and admission resumable;
- force may cancel queued work only;
- do not use Nix process-global interrupts for per-request cancellation;
- `shutdownConnections()` is future emergency behavior, not ordinary control
  flow.

Rely on Nix locks for build/GC/SQLite correctness. Do not add a Python global
maintenance lock initially.

## Focused test program

Only run the dedicated multithreading group during this implementation.

### Evaluator and GC

- several concurrent EvalSessions on one Store;
- distinct evaluator thread identities;
- concurrent evaluation, forcing, navigation, calls, releases, and close;
- repeated open/evaluate/close cycles and long stress runs;
- use-after-close rejection.

### Real Store work

- concurrent cache-miss `query_path_info`, `read_derivation`, closure, and
  missing-path operations;
- Store work while evaluators force values;
- LocalStore and RemoteStore variants where available.

### Explicit builds and IFD

- independent derivations with `build_workers=1` and greater-than-one limits;
- actual metadata queries while builds are in flight;
- real IFD/string-realisation fixture;
- assert IFD blocks only its owner evaluator and shares build-gate capacity
  with explicit builds;
- same-canonical-Store build avoids unexpected copy-closure behavior.

### Logging and close behavior

- concurrent evaluator/query/build logs have correct operation IDs;
- nested Nix Activity parent hierarchy remains intact;
- worker-thread context restores between jobs;
- close with queued/running work, `wait=False`, timeout, and force semantics;
- Store close handles every dependent EvalSession.

## Implementation order

1. Stabilize executor, admission, task tracking, and logger-context primitives.
2. Add Boehm bridge and dedicated evaluator lane; add evaluator stress tests.
3. Add `PyEvalState.buildStore` support and C++ BuildGateStore.
4. Rework Store methods into atomic query/build submissions; remove raw call.
5. Add `Value.get_derived_path()` and Store-owned DerivedPath build APIs.
6. Implement lifecycle/cancellation contract and concurrency diagnostics.
7. Add real Store/build/IFD/logging tests and iterate until robust.
8. Only then run repository-wide quality checks and update public docs/examples.

## Current working tree

Existing uncommitted changes are an early partial implementation:

- configurable multi-worker `NixThreadExecutor`;
- Session Store pool;
- one executor per EvalSession;
- initial `test_inproc_multithreaded_poc.py` tests.

They demonstrate basic overlap but are not the target architecture. Rework
rather than preserve them blindly: generic evaluator executors lack Boehm
lifecycle hooks, Store methods split native work across tasks, the logger ID
races, `PyEvalState` lacks `buildStore`, and tests do not yet cover real
Store/IFD/build/logging behavior.
