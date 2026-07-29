# nanopynix code improvement plan

Written after a read of the Python sources, the proto schemas, the test tree,
the packaging and the CI definition, plus ten measurement probes against a
running Nix 2.34. Each finding names the file and the symbol it comes from.
Each measurement gives the command and the result.

The prose follows ASD-STE100, as `AGENTS.md` requires.

---

## 1. Executive assessment

### Maturity

The Python code is more mature than most bindings projects of this size. The
error taxonomy, the parity discipline between the two engines, the gRPC status
trailer codec, and the C++ lifetime notes in the executor are the work of
somebody who has already found the hard failures. `pyright --strict` reports
zero errors over 13.3 kLOC. Both ruff configurations report zero findings. The
suite has 1361 test functions and collects 2537 cases across two store
backends.

The weakness is not the code that exists. It is the distance between what the
documentation promises and what a caller gets. The largest defect in this
report is a headline API that raises on every use outside one of its five
scopes, and no test constructs it that way.

### Strongest architectural qualities

1. **The `_core` layer is a real boundary.** `_core/_objects.py` holds the
   Nix pointers, and neither engine's transport reaches into it. `_core`
   imports nothing from `nanopynix.rpc` or `nanopynix.inproc`. Verified.
2. **The parity ledger is enforced, not aspirational.**
   `tests/nanopynix/test_engine_parity.py` fails on an unlisted difference
   *and* on a listed difference that has stopped occurring. That second half
   is what keeps a ledger from becoming a rubber stamp.
3. **Errors keep their structure across the process boundary.**
   `rpc/_status_details.py` packs `nix::ErrorInfo` into a `google.rpc.Status`
   trailer, trims it to a byte budget when a trace is deep, and marks what it
   dropped. An rpc caller's `NixError.info` compares equal to an inproc
   caller's.
4. **The version-gate mechanism works.** One model serves Nix 2.31, 2.34 and
   2.35. The drift check compares each model against Nix's own live registry,
   and it is clean on all four settings surfaces and all eleven store models.
   Measured this session.
5. **The comments record evidence, not intent.** `_core/_nix_executor.py`
   explains the 60 MiB pthread stack with the Nix source line it replaces.
   That style makes the code changeable by somebody who did not write it.

### Most important risks

1. **The configuration API raises on four of its five scopes** (Finding 1).
   This is the headline object, the documented example fails, and no test
   covers it.
2. **A documented safety guard does not exist in the worker** (Finding 2).
   The docstring says the worker re-checks. It does not.
3. **The in-process engine never frees a Nix value** (Finding 3). Measured:
   1501 rooted values retained after three calls that a caller believes it
   released.
4. **Crash isolation is the RPC engine's stated reason to exist, and no test
   kills a worker** (Finding 10).
5. **No lint, type or format gate runs in CI** (Finding 8). Three clean gates
   are maintained by hand.

### The five changes with the highest expected impact

| # | Change | Finding | Size |
|---|---|---|---|
| 1 | Route each settings scope to the Nix door that accepts it, and make the router the only path | 1 | L |
| 2 | Give the worker the guard its client already has, and delete the unreachable copy | 2 | S |
| 3 | Give `inproc.Value` the release-on-collect behaviour `rpc.ValueProxy` already has | 3 | M |
| 4 | Add a `checks` flake output that runs pyright, both ruff configurations and the drift checks; wire it into CI | 8 | M |
| 5 | Kill a worker process, in three ways, and assert what the caller sees | 10 | M |

---

## 2. Architecture map

### Layers, from the bottom

```
nix C++ (libstore, libexpr, libfetchers, libflake)
  │
  ├─ nanopynix-bindings/src/*.cpp          nanobind, 5.6 kLOC C++
  │    nix_util.cpp    globalConfig, logger, verbosity, experimental features
  │    nix_store.cpp   Store, StorePath, StoreReference, store-type registry
  │    nix_expr.cpp    EvalState, Value, RootValue, primop registration
  │    nix_flake.cpp   lockFlake, callFlake, flake settings registry
  │    nix_errors.cpp  one translator for the whole nix::Error hierarchy
  │    py_store_impl.* a nix::Store subclass that dispatches into Python
  │
  ├─ nanopynix/_core/                      transport-neutral, synchronous
  │    _nix_core.py    NixCore: initialize, settings, open_store, open_eval_state
  │    _objects.py     CoreStore, CoreEvalState, CoreValue, CoreLockedFlake, CoreRuntime
  │    _nix_executor.py NixThreadExecutor: one pool, 60 MiB stack, GC thread hooks
  │
  ├─ nanopynix/{settings,stores,models,exceptions,protocols,logging}.py
  │                                        typed surface, shared by both engines
  │
  ├─ nanopynix/inproc/_impl.py             engine A: this process
  └─ nanopynix/rpc/                        engine B: a worker subprocess
       client/session.py   Session facade
       client/_pool.py     WorkerClient: forkserver, gRPC channel, log relay
       client/store.py     Store + StoreHandle
       client/_session.py  EvalSession, ReplSession, ValueProxy, lease machinery
       worker/_worker.py   WorkerState, WorkerServiceHandler, service factory
       worker/_worker_store.py, _worker_eval.py   the two other gRPC handlers
       _status_details.py  google.rpc.Status trailer codec (both ends)
```

Dependency direction is clean, with one deliberate exception.
`rpc/client/_pool.py:48` imports `nanopynix.rpc.worker._worker` for
`worker_service_factory`. The forkserver pickles that function, so it must be
a module-level name the client can reach. Nothing else crosses.

### Operation A — `store.query_path_info(path)`, in process

1. `inproc.Store.query_path_info` (`inproc/_impl.py:637`) calls
   `Session.run`.
2. `Session.run` (`:372`) allocates an operation id, tags it into every active
   `LogCapture`, and submits to the store pool.
3. `NixThreadExecutor.run` (`_nix_executor.py:143`) submits to a
   `ThreadPoolExecutor`, then awaits through `asyncio.wrap_future`.
4. `_run_with_log_context` (`inproc/_impl.py:166`) sets the thread-local
   logger request id, calls the function, and translates a raw binding
   exception into the public hierarchy.
5. `CoreStore.query_path_info` (`_core/_objects.py:238`) normalises the path
   and calls the binding.
6. The binding returns a dict. `PathInfo(**result)` validates it.

### Operation A' — the same call over RPC

1. `rpc.Store.query_path_info` calls `StoreHandle._rpc_proxy_call`
   (`rpc/client/store.py:157`), which stamps `store_handle` onto the message.
   There is no lock: store calls may overlap.
2. `WorkerClient.invoke` (`_pool.py:354`) allocates the request id, tags the
   active captures, and dispatches the gRPC stub call.
3. In the worker, `wrap_service_handlers` has wrapped the handler with
   `convert_handler_errors` (`worker/_grpc_util.py:20`).
4. `worker_op` (`:62`) turns the synchronous handler body into an async entry
   point that dispatches through `WorkerState.run_request`.
5. `run_request` (`worker/_worker.py:181`) sets the same thread-local logger
   id and runs on a thread bounded by a four-slot `anyio.CapacityLimiter`.
6. The same `CoreStore.query_path_info` runs. Its result is shaped into the
   response message.
7. On failure, `details_for_exception` packs `nix::ErrorInfo` into the status
   trailer, and `from_response` on the client rebuilds the same exception
   class the inproc caller would have seen.

The two paths meet at step 5/7: **`CoreStore` is the shared implementation**,
not a duplicated one.

### Operation B — `session.eval(store, eval_settings=...)`

Both engines render the model to a `dict[str, str]` and pass it to
`CoreRuntime.open_eval_state`, which reaches the `EvalState` constructor
(`_core/_objects.py:715`). This is the correct door: Nix reads those settings
while it builds the evaluator.

`nix_path` is removed from that map first and travels in its own argument
(`inproc/_impl.py:970`, `rpc/client/_session.py:958`), because Nix builds the
search path from a constructor argument rather than from the settings map.

### Operation C — the settings a `Session` is opened with

`Session.__init__` calls `NixSettings.to_worker_settings()` and hands the
whole flat map to `NixCore.initialize`, which applies each entry with
`nanopynix_util.set_setting` (`_core/_nix_core.py:92`). That binding is
`nix::globalConfig.set()` (`nanopynix-bindings/src/nix_util.cpp:38`).

**This is where the architecture and the model disagree.** See Finding 1.

### Value ownership, by engine

| | `inproc.Value` | `rpc.ValueProxy` |
|---|---|---|
| Backing resource | a rooted `CoreValue` on the evaluator thread | an int handle in the worker's `HandleRegistry` |
| Registered with | `CoreEvalState._values`, a strong `set` | `_DeferredReleases`, plus a `weakref.finalize` |
| Freed on explicit release | yes | yes |
| Freed when the Python object is collected | **no** | yes, drained at the next RPC |
| Freed at evaluator close | yes | yes |

---

## 3. Findings

Confirmed defects come first. Architectural improvements follow. Stylistic
items come last.

---

### 3.1 Confirmed defects

---

#### F1 — `Session(settings=NixSettings(...))` raises for every field outside the global scope

* **Category:** API correctness, configuration routing
* **Severity:** critical
* **Confidence:** confirmed

**Evidence**

`NixSettings` inherits five scopes (`settings.py:597`). `to_worker_settings()`
renders every set field of all five into one flat mapping. `Session.__init__`
passes that mapping to the worker, and `NixCore.initialize` applies each entry
with `set_setting`, which is `nix::globalConfig.set()`.

Measured with one process per case, rpc engine (`scratchpad/probe3.py`):

```
max_jobs             OK
trusted              FAIL  RuntimeError: unknown setting: trusted
pure_eval            FAIL  unknown setting: pure-eval
allowed_uris         FAIL  unknown setting: allowed-uris
tarball_ttl          FAIL  unknown setting: tarball-ttl
accept_flake_config  FAIL  unknown setting: accept-flake-config
priority             FAIL  unknown setting: priority
want_mass_query      FAIL  unknown setting: want-mass-query
path_info_cache_size FAIL  unknown setting: path-info-cache-size
max_call_depth       FAIL  unknown setting: max-call-depth
restrict_eval        FAIL  unknown setting: restrict-eval
warn_dirty           FAIL  unknown setting: warn-dirty
```

The inproc engine fails identically (`scratchpad/probe1.py`).

The cause is not an ordering mistake. `globalConfig` and the three other
registries are **disjoint** in every supported Nix (`scratchpad/probe5.py`):

```
global count 85
eval  ∩ global: []
fetch ∩ global: []
flake ∩ global: []
store-default names in global: []
```

`set_setting("pure-eval", ...)` fails the same way before and after
`init_libexpr()` (`scratchpad/probe4.py`), so moving the call does not help.

**Why it matters**

1. The documented example fails. `docs/nanopynix/api/settings.md:41` shows
   `NixSettings(max_jobs=4, trusted=True, pure_eval=True)`.
2. `NixSettings`' own docstring says each field "reaches the place that can
   honour it". Four scopes reach nothing.
3. The store half of the promise works. `NixStoreDefaults.from_settings`
   feeds `resolve_store_spec`, which writes the four store settings into each
   store URI. The eval, fetch and flake halves are not implemented at all:
   neither `inproc.EvalSession.open` nor its rpc counterpart merges a
   session-level default into `open_eval_state`.
4. The comment in `check_settings_model_drift` (`settings.py:723-730`) states
   that `globalConfig` aggregates the evaluator, fetcher and flake settings.
   The measurement above disproves it, and the filtering step it justifies
   removes nothing.

**Why no test caught it.** Every `NixSettings(...)` in `tests/` and in
`docs/examples/` names global-scope fields only. The one test that uses eval
settings passes them to `session.eval(...)`, which is the working path.

**Recommended direction**

Make `NixSettings` a *router*, and make the router the only way settings
leave it. Concretely:

* Add `NixSettings.for_scope(scope)` returning the scope model, built by
  `model_validate(self.model_dump(include=set(Scope.model_fields), exclude_none=True))`
  — the mechanism `NixStoreDefaults.from_settings` already uses. Generalise
  that classmethod onto `NixConfigModel` so all five scopes share it.
* `Session.__init__` sends `for_scope(NixGlobalSettings).to_worker_settings()`
  to `initialize`, and keeps the other four as session defaults.
* `Session.eval` / `Session.repl` merge the session's eval and fetch defaults
  *under* the per-evaluator argument, exactly as `resolve_store_spec` merges
  store defaults under a store model.
* `EvalSession.lock_flake` / `eval_flake` merge the session's flake defaults
  the same way.
* Delete the disproved comment in `check_settings_model_drift` and the dead
  filtering it justifies. Keep the function; the four surfaces are still
  compared against four registries.

**Tradeoffs and alternatives**

An alternative is to reject a non-global field in `Session.__init__` with a
clear message. That is smaller and honest, but it removes the catch-all the
design chose deliberately, and it leaves a caller with no way to set a
session-wide `pure_eval`. Choose the router. Only fall back to rejection if
the maintainer decides session-wide eval defaults are not wanted.

**Verification**

A parametrised test that opens a session with exactly one field set, one case
per scope, and then reads the value back through the door that owns it:
global through `session.settings()`, store through `store.uri(with_params=True)`,
eval and fetch through observable behaviour (a `max_call_depth` breach, a
`pure_eval` rejection), flake through a flake operation. The test must run on
both engines and both backends.

---

#### F2 — The worker does not enforce the construction-time settings guard it is documented to enforce

* **Category:** boundary safety, RPC contract
* **Severity:** high
* **Confidence:** confirmed

**Evidence**

`reject_construction_time_keys` (`settings.py:101`) states: "Both engines
check before they send, and the worker checks again, so nothing can slip past
by building a request by hand."

`NixCore.configure_eval_state` (`_core/_nix_core.py:154`) is the function that
performs the worker-side check. It has no production caller.
`tests/nanopynix/core/test_nix_core_unit.py:1-7` says so in its own module
docstring: "nothing currently calls configure_eval_state".

The worker's handler calls the *other* method.
`EvalServiceHandler.configure_eval` (`rpc/worker/_worker_eval.py:259`) calls
`CoreEvalState.configure` (`_core/_objects.py:493`), which applies both maps
with no check at all.

Measured (`scratchpad/probe6.py`):

```
public configure(pure_eval=True):      refused (SettingNotLiveError)
hand-built ConfigureEval(pure-eval):   ACCEPTED by worker
```

The existing unit test does not close the gap either: it passes `{"a": "1"}`,
which is not a Nix setting, so it would pass whether the guard ran or not.

**Why it matters**

The whole configuration layer exists to stop Nix from accepting a setting and
discarding it. The client guard is the one that gives a good message. The
worker guard is the one that makes the property true of the *protocol* rather
than of one client. A second implementation of the client, or a caller reaching
`session._manager.eval_stub` directly, gets the old silent-drop behaviour.

**Recommended direction**

Delete `NixCore.configure_eval_state` and put the check where the call already
goes: at the top of `CoreEvalState.configure`. One guard, one call site, and
the `EvalSettingsTarget` protocol and its static-assertion test disappear with
it.

Then rewrite `tests/nanopynix/core/test_nix_core_unit.py` to assert the
refusal with a real construction-time key, and add an rpc-level test that
sends a hand-built `ConfigureEvalRequest` and expects a refusal.

**Tradeoffs**

`CoreEvalState` currently has no dependency on the settings module beyond
`SettingsProvenance` in the same file's `CoreRuntime`. Adding the guard adds
one import inside `_core`. That is correct: the construction-versus-live
distinction is a property of Nix, not of a transport.

**Verification**

`probe6.py`'s second case must report a refusal.

---

#### F3 — The in-process engine never releases a rooted Nix value when the Python handle is collected

* **Category:** native resource ownership
* **Severity:** high
* **Confidence:** confirmed

**Evidence**

`CoreEvalState._values` is a strong `set` (`_core/_objects.py:382`).
`wrap_value` adds to it; only `CoreValue.close()` removes. `inproc.Value`
(`inproc/_impl.py:1329`) has no `weakref.finalize` and no `__del__`.

Measured on a 500-attribute attrset (`scratchpad/probe9.py`):

```
inproc rooted after eval_string: 1
  after as_dict: 501    after drop+gc: 501
  after as_dict: 1001   after drop+gc: 1001
  after as_dict: 1501   after drop+gc: 1501
```

Dropping every Python reference and running `gc.collect()` reclaims nothing.

The rpc engine does the opposite. `ValueProxy.__init__` installs
`weakref.finalize(self, _finalize_lease, ...)` (`rpc/client/_session.py:404`),
`_ensure_resolved` installs one for a lazily-selected child (`:464`), and
`EvalProxy._drain_deferred_releases_locked` (`:298`) sends the release RPCs at
the next call. `scratchpad/probe10.py` confirms the drain runs.

**Why it matters**

Each retained `CoreValue` holds a Nix `RootValue`, which is a Boehm GC root.
It therefore pins everything reachable from it in the Nix heap, not just one
cell. `Value.as_dict()` and `Value.as_list()` root every child in one call, and
both are the documented way to read a value Nix cannot flatten to JSON.

The consumers most exposed are the long-lived ones: `pynix`'s REPL and its
LSP server, which hold one evaluator for the life of a process.

This is also an engine asymmetry that process isolation does not force, which
is the repository's own definition of a defect
(`tests/nanopynix/test_engine_parity.py` module docstring).

**Recommended direction**

Give `inproc.Value` the same shape rpc already has:

* Make `CoreEvalState._values` a `weakref.WeakSet` of `CoreValue`, so the
  evaluator's close-everything sweep keeps working without the set being the
  reason a value lives.
* Register a `weakref.finalize` on `inproc.Value` that pushes its `CoreValue`
  onto a per-evaluator release queue. Do not call Nix from the finalizer: it
  can run on any thread, and `CoreValue.close()` must run on the evaluator
  thread.
* Drain that queue at the start of `EvalSession.run`, which is the single
  chokepoint every evaluator operation already passes through. That mirrors
  `EvalProxy._drain_deferred_releases_locked` exactly.

**Tradeoffs**

A `WeakSet` changes when a value is destroyed relative to evaluator close. A
value still referenced by the caller at close time must still be closed by the
close sweep, so keep that sweep and let the weak set report only live ones.
The alternative — releasing eagerly in `as_dict`/`as_list` — is wrong, because
those return handles the caller is meant to use.

**Verification**

Re-run `probe9.py`. The count after `drop+gc` plus one evaluator operation must
return to 1. Add it as a test with an explicit number, marked as measuring
resource release rather than behaviour.

---

#### F4 — `inproc.Session.close()` swallows cancellation

* **Category:** cancellation safety
* **Severity:** medium
* **Confidence:** confirmed by reading; not yet reproduced

**Evidence**

`inproc/_impl.py:324-329`:

```python
async def close_resource(operation: Any) -> None:
    try:
        await operation
    except BaseException as exc:
        errors.append(exc)
```

`CancelledError` is a `BaseException`. It lands in `errors` and leaves as a
plain raise or as a `BaseExceptionGroup` (`:331-334`). The `executor.shutdown`
handler at `:345` does the same thing.

`inproc/_impl.py` has three other `except BaseException` handlers, at `:267`,
`:981` and `:1227`. **Those three are correct** and must not be changed: each
runs a cleanup and then re-raises, which is the one shape that must catch a
cancellation. The comment at `:981` explains why its cleanup cannot be skipped
— an abandoned thread stays registered with the Boehm GC. Only the two
handlers that *collect* into `errors` are wrong.

The rpc engine catches `Exception` only, and its comment
(`rpc/client/session.py:256-262`) states the reason: converting a cancellation
means the scope that owns it never sees it, so an enclosing `fail_after`
cannot turn its own expiry into a `TimeoutError`. `_pool.py:287-302` records
that this exact bug was once present there and was removed.

**Why it matters**

An `inproc.Session.close()` inside a cancelled task, or inside a
`move_on_after`, does not cancel. The caller sees an exception group instead of
the cancellation it asked for, and structured concurrency stops working around
it.

**Recommended direction**

Catch `Exception`, not `BaseException`, in `close_resource`. Keep the separate
`except BaseException` around `executor.shutdown` if the maintainer wants a
shutdown failure recorded, but let a cancellation through.

**Tradeoffs**

A cancellation now abandons the remaining resources. That is the correct
trade: `close` already runs `executor.shutdown(wait=True)` in a `finally`, so
the Nix thread still stops. If more must survive cancellation, shield that
part explicitly, as `WorkerClient.close` does.

**Verification**

A test that cancels the task running `close()` and asserts that
`CancelledError` propagates, plus one that wraps `close()` in
`anyio.fail_after` and asserts `TimeoutError`.

---

#### F5 — Documentation code is never executed, and two published examples do not run

* **Category:** documentation, verification
* **Severity:** medium
* **Confidence:** confirmed

**Evidence**

`tests/nanopynix/test_examples.py:19` runs `docs/examples/*_example.py` only.
No test executes a fenced code block in a Markdown file.

Two published snippets are wrong:

* `README.md:15` — `nanopynix.rpc.Session(config={"max-jobs": "4"})`. No
  `config` parameter exists on either engine.
* `docs/nanopynix/api/settings.md:41` — the F1 example.

**Recommended direction**

Extract every runnable Markdown block into `docs/examples/` and include it by
literal reference, or add a doctest-style collector that executes fenced
`python` blocks in `README.md` and `docs/**/*.md`. The first is simpler and
matches the existing mechanism.

**Verification**

The suite fails when a published snippet stops working.

---

### 3.2 Architectural improvements

---

#### F6 — `WorkerDiedError` sits outside the exception taxonomy and outside the package surface

* **Category:** error taxonomy, API design
* **Severity:** medium
* **Confidence:** confirmed

**Evidence**

`rpc/client/_pool.py:94` defines it as a bare `RuntimeError`. It is not a
`NixError`, not an `ObjectMisuseError`, and not in `nanopynix.__all__`. A
caller reaches it only as `nanopynix.rpc.WorkerDiedError`, from a package whose
`__init__` re-exports it out of a private module.

`docs/nanopynix/architecture.md` names it as the RPC engine's headline
behaviour: "a worker crash/OOM raises `WorkerDiedError` — your process
survives."

**Why it matters**

`nanopynix.exceptions`' module docstring sets out a deliberate taxonomy: Nix
errors, then object misuse. Worker death is a third thing — an infrastructure
failure of the transport — and it has no home. A caller writing
`except nanopynix.NixError` around an rpc call is not protected against the
failure mode the engine exists to produce.

The same gap covers the other transport failures: `TimeoutError` from
`grpclib`, and `SessionClosedError`, which *is* in the taxonomy.

**Recommended direction**

Add an `EngineError(RuntimeError)` base to `nanopynix.exceptions`, with
`WorkerDiedError(EngineError)` under it. Export both from `nanopynix`. Keep
`nanopynix.rpc.WorkerDiedError` as an alias so nothing breaks. Document in the
class docstring that the inproc engine cannot raise it, and why.

**Tradeoffs**

`nanopynix.exceptions` would then name a class only one engine raises. That is
acceptable and already true in spirit — `SessionClosedError` is shared, but the
*reason* differs. State the asymmetry in the docstring rather than hiding the
class in a private module.

---

#### F7 — The worker's handle registry is untyped, and the whole worker layer is `Any` to the type checker

* **Category:** type safety
* **Severity:** medium
* **Confidence:** confirmed

**Evidence**

`HandleRegistry.get_typed(handle, expected_kind) -> Any`
(`rpc/worker/_handle_registry.py:42`). The kind tag is checked at runtime and
discarded statically. `EvalServiceHandler.__init__(self, state: Any)`
(`_worker_eval.py:196`), and `StoreServiceHandler` the same.

The symptom is visible: `_worker.py:555-567`, `_shutdown_worker`, carries nine
suppressions on eight lines, each reading "cascade from WorkerState `Any`
attributes" — although `WorkerState`'s attributes are all concretely typed.

**Why it matters**

`pyright --strict` reports zero errors over this code because it can see
nothing to check. Every handler body — the code that turns a wire message into
a Nix call — is unverified. That is the layer where a wrong handle kind or a
wrong attribute name becomes a `TypeError` in a subprocess.

**Recommended direction**

Two mechanical changes, in order:

1. Type `state` as `WorkerState` on all three handlers. `WorkerState` is
   defined in `_worker.py`, and the handlers are imported *by* `_worker.py`,
   so move `WorkerState` to a neutral module (`rpc/worker/_state.py`) to break
   the cycle. `AGENTS.md` names that as the preferred fix.
2. Make the registry generic:
   `def get_typed[T](self, handle: int, expected_kind: HandleKind, cls: type[T]) -> T`
   with an `isinstance` check, or one small typed accessor per kind
   (`get_store`, `get_eval_entry`, `get_value`, `get_locked_flake`). Prefer
   the accessors: there are four kinds, they never change, and the call sites
   read better.

Delete `HandleRegistry.clear()` while there. It has no caller, and it resets
`_next = 1`, so using it would reissue handle numbers a client still holds.

**Verification**

`pyright` stays at zero, and the suppression count in `_worker.py` drops from
16 to single digits. Count them before and after.

---

#### F8 — Session-scoped defaults exist for stores and for nothing else

* **Category:** API design, consistency
* **Severity:** medium
* **Confidence:** confirmed
* **Depends on:** F1

**Evidence**

`resolve_store_spec` (`stores.py:529`) merges `NixStoreDefaults` under a store
model, and both engines call it. There is no equivalent for evaluator, fetcher
or flake settings. `EvalSession.open` on both engines reads only the
per-evaluator argument.

**Why it matters**

This is the second half of F1, and it is worth stating separately because it
survives whichever way F1 is resolved. A caller who wants every evaluator in a
session to be pure has to repeat `eval_settings=` at every `session.eval(...)`
call, and there is no way to state it once.

**Recommended direction**

One merge helper, used four times:

```python
def merge_defaults[M: NixConfigModel](spec: M | None, defaults: M | None) -> M | None:
    """Fill the fields ``spec`` did not set from ``defaults``. A set field wins."""
```

`resolve_store_spec` becomes its store-shaped caller. `Session.eval`,
`Session.repl`, `EvalSession.lock_flake` and `EvalSession.eval_flake` become
the other three.

---

#### F9 — The public surface disagrees with itself

* **Category:** API design, packaging
* **Severity:** medium
* **Confidence:** confirmed

**Evidence** (`scratchpad/probe7.py`, `probe8.py`)

* 20 names are importable from `nanopynix` but absent from its `__all__`.
  Among them: `EvalError`, `EvalSessionClosedError`, `ForeignValueError`,
  `InfiniteRecursionError`, `MissingArgumentError`, `LogCapture`,
  `LogCollector`, `LogEvent`, `Derivation`, `BuildResult`, `EvalState`,
  `DEFAULT_EXPERIMENTAL_FEATURES`.
* 3 exception classes defined in `exceptions.py` are missing from *its*
  `__all__`: `ListIndexError`, `MissingAttributeError`, `SettingNotLiveError`.
  The first two *are* in `nanopynix.__all__`, so the two lists disagree in
  both directions.
* `Session.subscribe(self, callback: Any) -> Any` (`rpc/client/session.py:345`)
  is untyped, although the `WorkerClient.subscribe` it delegates to is fully
  typed, and **the inproc engine's own `subscribe` returns `BusSubscription`**
  (`inproc/_impl.py:547`). The two engines therefore disagree on the return
  type of a method the parity ledger treats as shared.
* `Nix = Session`, labelled "Backward-compatible alias"
  (`rpc/client/session.py:467`), is exported from `nanopynix.rpc.__all__` and
  used by one test. `ROADMAP.md` states backwards compatibility is not a
  priority.
* `inproc.Store.call(method, *args, **kwargs) -> Any` (`inproc/_impl.py:908`)
  is an untyped L1 escape hatch with one caller, a test. `CoreEvalState.__getattr__`
  (`_core/_objects.py:397`) is the same hole one layer down, and its own
  docstring says the `CoreStore` equivalent has already been removed.

**Why it matters**

`EvalError` is the base of half the exception tree, and
`except nanopynix.EvalError` is what a caller reaches for first. It is
importable, it works, and it appears in no `__all__` and in no generated
document. A name in that state is neither supported nor removable.

**Recommended direction**

* Decide one rule: **`__all__` is the public surface, and every entry has a
  documented home.** Add the names that belong (`EvalError` and the four other
  exceptions, `LogCapture`, `LogEvent`). Make the rest private by renaming or
  by removal (`_init_libstore_raw` is already `_`-prefixed and correct).
* Make `exceptions.__all__` complete, and add a test that asserts every
  exception class defined in the module appears in it. That test is four lines
  and prevents the class of drift entirely.
* Type `Session.subscribe` as `WorkerClient.subscribe` already is.
* Delete `Nix`, and update the one test.
* Delete `inproc.Store.call` and its test, or keep it and document it as
  unsupported. It duplicates a surface `AGENTS.md` says has already been
  retired on `CoreStore`.

**Verification**

Add `tests/nanopynix/test_public_surface.py` asserting: every name in
`nanopynix.__all__` resolves; every public name importable from `nanopynix` is
in `__all__`; every exception class in `nanopynix.exceptions` is in its
`__all__`; every name in `__all__` appears in `docs/nanopynix/api/`.

---

#### F10 — Worker-death coverage never kills a worker

* **Category:** test architecture
* **Severity:** medium
* **Confidence:** confirmed

**Evidence**

`tests/nanopynix/rpc/client/test_workerpool.py:98`,
`test_worker_death_detection`, force-closes the gRPC *channel*. Its own comment
says: "In multiprocessing mode, the worker is managed by AsyncExitStack; kill
via process is not directly exposed." Nothing in the suite sends a signal to
the worker process.

**Why it matters**

Crash isolation is the RPC engine's documented reason to exist. Three distinct
failures are untested:

1. `SIGKILL` mid-call — what the OOM killer does.
2. A Nix-side abort inside the worker — the `SIGABRT` case that
   `nanopynix.init_libstore`'s docstring describes for experimental features.
3. Death while handles are outstanding — do `ValueProxy` finalizers queue
   releases against a dead channel, and does `Session.close()` still finish?

**Recommended direction**

`WorkerClient` already keeps `self._worker_proc` for exactly this
(`_pool.py:405-411`). Add a test-only accessor and three tests:

* kill during an in-flight `store.query_path_info` → `WorkerDiedError`;
* kill while an `EvalSession` holds resolved `ValueProxy` objects, then
  `session.close()` → no hang, no second exception, process reaped;
* `session.open()` on a fresh `Session` in the same process afterwards → works.

Mark them `concurrency` so the ThreadSanitizer matrix picks them up.

**Tradeoffs**

These tests are inherently racy. Bound each with `anyio.fail_after` and assert
the exception *class*, not the message.

---

#### F11 — A stalled log consumer stalls Nix, with no bound and no signal

* **Category:** backpressure, liveness
* **Severity:** medium
* **Confidence:** likely; the stall is by design, the unbounded duration is not

**Evidence**

`LogCollector.__init__` takes `maxsize=10_000`, and `callback`
(`logging.py:83-92`) does a blocking `sync_q.put`. The C++ logger calls it
after `gil_scoped_acquire`, from the Nix thread. The docstring says the block
is deliberate: "this deliberately backpressures the Nix logger callback instead
of dropping events."

The stall is unbounded. There is no timeout, no drop policy, and no counter of
time spent blocked. `stats()` reports depth only.

Three ways the consumer can stop: the caller's event loop is blocked in
synchronous work; `WorkerClient._teardown` cancels `_log_task` after
`_LOG_DRAIN_TIMEOUT_SECONDS` (`_pool.py:324-330`) while the worker may still
be emitting; or the client dies.

`LogCapture.events` (`logging.py:252`) is a plain list with no cap, so a build
inside `async with session.capture_logs()` accumulates every line.

**Why it matters**

A Nix evaluation or build that stops making progress because a log reader is
slow is very hard to diagnose from the caller's side. It presents as a hang in
`await`, with no error anywhere.

**Recommended direction**

Keep lossless as the default; make the failure visible and bounded.

* Give `LogCollector.callback` a bounded wait. On expiry, drop the event,
  increment a `dropped` counter, and log once per interval through Python's
  `logging`. Report `dropped` in `stats()` and in the `SIGUSR1` diagnostic
  dump, which already prints `stats()` (`_worker.py:133`).
* Give `LogCapture` a `max_events` parameter with a default, and record
  `truncated` when it fires. The `NixErrorInfo.truncated` field already sets
  the precedent for saying "this was cut" rather than lying.

**Tradeoffs**

Dropping events weakens the lossless guarantee. That is the right trade if the
alternative is an unbounded stall of the evaluator, and the counter keeps it
honest. Choose a generous timeout — seconds, not milliseconds — so a merely
slow consumer never trips it.

**Verification**

A test that subscribes a consumer which never drains, runs a Nix operation
that emits more than `maxsize` events, and asserts the operation completes and
`stats()["dropped"] > 0`.

---

#### F12 — The in-process engine has no internal interface; its four classes reach into each other's privates

* **Category:** module structure
* **Severity:** medium
* **Confidence:** confirmed (measurement), speculative (the proposed remedy)

**Evidence**

`inproc/_impl.py` is 1694 lines and holds `Session`, `Store`, `EvalSession`,
`ReplSession`, `LockedFlake` and `Value`. It carries 51 suppressions, **43** of
which are `reportPrivateUsage` or `SLF001` for cross-class private access. The
rpc client's two files carry 17 between them, and its largest single file is
1306 lines.

Typical lines: `self._session._evals.discard(self)`,
`self._eval_session._track_value(local)`, `store._require_core()`,
`eval_session._begin_close(force=force)`.

**Why it matters**

Each suppression is a place where the type checker was told to stop looking.
Forty-three of them in one file means the four classes form one object split
across four `class` statements. A change to any of them can break the others
without pyright noticing, and the suppression comments — which are individually
good — collectively hide that.

**Recommended direction**

This is a refactor, not a fix, so do it after the defects and behind the tests
that F1, F3 and F4 add.

Introduce the interfaces the private access is standing in for. Two are
obvious from the access patterns:

* An evaluator-lifecycle interface on `Session` (`claim_eval`, `release_eval`,
  `begin_close`, `drain`, `resume`) — rpc's `Session` already has
  `claim_eval`/`release_eval` as public methods, and inproc reaches
  `_evals` directly instead. Adopt rpc's spelling.
* A value-tracking interface on `EvalSession` (`track_value`, and the run
  chokepoint), replacing `_track_value`, `_run_closing` and `_next_operation_id`
  reached from `Value` and `LockedFlake`.

Split the file along the same seam once the interfaces exist:
`inproc/_session.py`, `inproc/_store.py`, `inproc/_eval.py`,
`inproc/_value.py`. Do not split first: splitting without the interfaces turns
private access into cross-module private access, which is worse.

**Tradeoffs**

The suppressions are individually justified and the code works. The gain is not
tidiness; it is that a future change to the ownership rules becomes checkable.
If the maintainer judges the cost too high, the acceptable smaller step is to
adopt rpc's `claim_eval`/`release_eval` spelling on inproc, which removes the
largest single group.

---

#### F13 — No lint, type or format gate runs in CI

* **Category:** tooling
* **Severity:** medium
* **Confidence:** confirmed

**Evidence**

`ci/workflows/lib.nix` builds test jobs only; each runs
`./result/bin/nanopynix-tests`. `rg -ln "pyright|ruff|treefmt" .github/ ci/`
matches one `# ruff: noqa` comment in `ci/render.py`. `flake.nix` exposes
`packages`, `devShells`, `legacyPackages` and `lib` — no `checks`.

Three gates are maintained by hand and are currently clean: `ruff check`,
`ruff check --config ruff-strict.toml`, `ruff format --check` (259 files), and
`pyright` (0 errors). All four verified this session.

**Why it matters**

`ruff-strict.toml` is 15 kLOC of considered configuration and `AGENTS.md` says
"This configuration reports zero findings now. Keep it at zero." Nothing
enforces that but habit. The same applies to `pyright --strict`, which is the
project's main defence and the reason F7 matters.

**Recommended direction**

Add a `checks` output to `flake.nix` with four derivations — `lint`,
`lint-strict`, `format`, `types` — plus a fifth, `drift`, that runs
`check_all_settings_model_drift(include_optional=True)` and
`check_all_store_model_drift()` against each supported Nix version. Add one
fast CI job that builds `checks`.

Keep `treefmt` out of CI as a *writer*; the format gate is
`ruff format --check`, which does not write. `treefmt.toml`'s exclusions for
the LSP fixture tree are load-bearing and must not be reached by a check job.

**Verification**

The job fails on a deliberately introduced `ruff` finding and on a deliberately
introduced pyright error.

---

### 3.3 Smaller items

---

#### F14 — Dead and stale surface

* **Severity:** low · **Confidence:** confirmed

* The stdio worker entry point has no client.
  `nanopynix/pyproject.toml:43` declares
  `nanopynix-worker = "nanopynix.rpc.worker._worker:main"`, but `_pool.py`
  only uses `multiprocessing_worker_with_backchannel`. Its docstring
  (`_worker.py:599`) also names a module path that does not exist
  (`nanopynix._worker`; the real path is `nanopynix.rpc.worker._worker`).
  Either give it a client and a test, or delete `main`, `_stdio_main` and the
  entry point. Deleting also removes the `serve_stdio` import.
* `HandleRegistry.clear()` — no caller; unsafe if used. See F7.
* `Nix = Session` — see F9.
* `check_all_settings_model_drift(include_optional=False)` checks only the
  global surface by default, and no default reaches
  `check_all_store_model_drift`. The drift check job (F13) should call both
  with everything switched on.

---

#### F15 — `NIX_USER_CONF_FILES` is set into the environment and never restored

* **Severity:** low · **Confidence:** confirmed

`inproc/_impl.py:259-260` and `rpc/worker/_worker.py:250-251` assign
`os.environ[NIX_USER_CONF_FILES_ENV]`. In the worker that is correct and
documented: the variable is an input Nix reads in `loadConfFile`, and the
process is disposable. In the in-process engine it is a permanent global side
effect of opening one session, which outlives that session and affects any
later Nix use in the same process.

Restore the previous value in `Session.close`, or state in the `nix_conf`
parameter docstring that inproc changes the process environment permanently.
Prefer the restore.

---

#### F16 — `nix_type_from_string` is patched onto a generated enum at import time

* **Severity:** low · **Confidence:** confirmed

`models.py:278-282` attaches a classmethod to the betterproto2-generated
`NixType` and suppresses the resulting type error. Every caller then needs
`# type: ignore[attr-defined]` too (`inproc/_impl.py:1468`).

The module comment argues the case well, and it is right that a subclass would
not be the class arriving from the wire. A plain module-level function
`nix_type_from_name(value: str) -> NixType` avoids both the patch and the
suppressions, at the cost of one import at each of its three call sites.
Consider it during the F9 surface pass; it is not worth a change of its own.

---

## 4. Target architecture and engineering principles

The current architecture is right. Nothing here proposes replacing it. The
target below states the rules the code already mostly follows, so that the
places it does not are visible as work rather than as taste.

### Layer boundaries and dependency direction

```
bindings  →  _core  →  {settings, stores, models, exceptions, protocols, logging}  →  {inproc, rpc}
```

Dependencies point right to left only. Two rules:

1. **`_core` never imports an engine.** Holds today; keep it.
2. **An engine never imports the other engine.** Holds today; keep it.

One exception is permanent and must stay documented at the import site:
`rpc/client/_pool.py` imports `rpc/worker/_worker.worker_service_factory`,
because the forkserver pickles it by module path.

### Public versus internal

* `nanopynix.__all__` is the public surface. Every entry has a page under
  `docs/nanopynix/api/`. Every public name importable from `nanopynix` is in
  `__all__`. A test enforces both directions (F9).
* `nanopynix.rpc` and `nanopynix.inproc` are the two engine surfaces, and they
  are symmetric by the rule the parity ledger already states: **process
  isolation is the only thing rpc has that inproc does not, so an asymmetry is
  a defect unless process isolation forces it.**
* A module named with a leading underscore is internal. `pynix` may depend on
  one only with a comment at the import site saying why, as `AGENTS.md`
  requires.

### Native resource ownership

State the rules once, in `_core/_objects.py`'s module docstring, and make the
code obey them on both engines:

1. **A `CoreValue`, a `CoreEvalState` and a `CoreLockedFlake` are confined to
   one evaluator thread.** Every call reaches them through that evaluator's
   `NixThreadExecutor`.
2. **A `CoreStore` is thread-safe** and may be shared by the store pool.
3. **Every native resource has exactly one owner**, and the owner is the
   object that created it.
4. **Dropping the Python handle releases the native resource**, on both
   engines, without the caller doing anything. Explicit `release()` is the
   deterministic form of the same thing, and closing the evaluator is the
   backstop. Today rpc obeys this and inproc does not (F3).
5. **A finalizer never calls Nix.** It queues, and the next operation on the
   owning thread drains the queue. rpc does this; inproc should.

### Process ownership

* One `Session` owns one worker process. `WorkerClient` owns its lifetime and
  is the only thing that may stop it.
* Teardown is always reached. The polite half (`Shutdown`) is cancellable; the
  teardown half runs under a shield and is separately bounded. This is already
  correct in `WorkerClient.close` and is the pattern to copy.
* A worker that outlives its teardown is terminated and then killed
  (`_stop_worker_process`). That path is correct and untested (F10).

### Error taxonomy

Four families, and every exception belongs to exactly one:

| Family | Base | Meaning |
|---|---|---|
| Nix said no | `NixError` | Nix was consulted and reported a failure |
| You misused an object | `ObjectMisuseError` | nanopynix refused before reaching Nix |
| The engine failed | `EngineError` *(new, F6)* | the transport or the worker failed |
| Python's own | `TimeoutError`, `ValueError`, … | ordinary Python conditions |

`NixError` keeps its three boundaries (A: nanobind type name, B: worker
message prefix plus status trailer, C: daemon prose). That model is sound and
documented; leave it.

### Transport-independent domain models

`nanopynix.models` re-exports the betterproto2 messages as the canonical data
types, and both engines produce the same objects. Keep it. Do not introduce a
second, "pure Python" model layer beside them; the wire type *is* the domain
type here, and the one place that needed more (`LogEvent`) is handled by a
subclass rather than by a parallel hierarchy.

### The in-process and RPC behavioural contract

The two engines must agree on: exception class, exception message where Nix
authors it, `NixError.info` contents, value laziness, settings acceptance and
refusal, and resource release timing. They may differ on: timeouts (rpc only —
an in-process call has no such failure mode), crash isolation, custom primops,
and the number of concurrently open sessions per process.

`test_engine_parity.py` checks names. `test_engine_parity_semantics.py` checks
behaviour. Both must keep growing; F3 and F1 are each a semantics entry that
was missing.

### Sync and async policy

* Public API is async on both engines. `_core` is synchronous and thread-
  confined. That split is correct and should not move.
* Inside async code, use the anyio primitive. The two documented exceptions
  (`asyncio.wrap_future` in `_nix_executor.py`, and `asyncio.create_task` for
  a portal or scope whose entry and exit are in different tasks) stay, with
  their reasons at the call site.
* A lazy value selector is synchronous on both engines
  (`attr`, `list_get`). Everything that reaches Nix is a coroutine.

### Compatibility and versioning

* **Python API:** no compatibility promise before 1.0, per `ROADMAP.md`. State
  that in the README rather than keeping aliases such as `Nix`.
* **Nix versions:** one model per surface, gated per field with
  `nix_version_min` / `nix_version_removed`, checked by the drift check under
  each supported version's dev shell. This works; make it a CI gate (F13).
* **RPC protocol:** the worker is spawned by the client from the same
  installed package, so version skew cannot occur today. If the stdio entry
  point ever gains a client (F14), the protocol needs a version field in
  `InitRequest` and a compatibility rule. Until then, say so in
  `worker.proto`.

---

## 5. Prioritised implementation roadmap

Phases are ordered by dependency and risk, not by appeal. Every item names
its files, its size, and what proves it done.

### Phase 0 — Correctness and safety

---

**0.1 — Route each settings scope to the door that accepts it**

* **Scope:** Add a generic `for_scope` extraction to `NixConfigModel`
  (generalising `NixStoreDefaults.from_settings`). Send only the global scope
  to `NixCore.initialize`. Keep the other four as session defaults. Delete the
  disproved aggregation comment in `check_settings_model_drift` and the dead
  filtering it justifies.
* **Files:** `settings.py`, `inproc/_impl.py`, `rpc/client/session.py`,
  `_core/_nix_core.py`, `docs/nanopynix/api/settings.md`.
* **Depends on:** nothing.
* **Benefit:** the headline configuration API stops raising.
* **Risk:** medium. Touches the path every session opens through. The drift
  checks and the settings test group bound it.
* **Size:** L
* **Acceptance:** `Session(settings=NixSettings(<field>))` opens for one field
  from each of the five scopes, on both engines, on both backends.
* **Tests:** a parametrised test added to the existing
  `tests/nanopynix/test_config_flow.py`, one case per scope, asserting the
  value through the door that owns it. That file already covers the store
  scope and the construction-versus-live rule; the four other scopes are the
  missing half.

**0.2 — Merge session-level eval, fetch and flake defaults**

* **Scope:** One `merge_defaults` helper. `resolve_store_spec` becomes its
  store-shaped caller. `Session.eval`, `Session.repl`,
  `EvalSession.lock_flake` and `EvalSession.eval_flake` become the others.
* **Files:** `settings.py`, `stores.py`, `inproc/_impl.py`,
  `rpc/client/session.py`, `rpc/client/_session.py`.
* **Depends on:** 0.1.
* **Benefit:** the "session-wide default" promise becomes true for all four
  scoped surfaces, not one.
* **Risk:** low. A field set on the call always wins, so nothing that works
  today changes.
* **Size:** M
* **Acceptance:** a session opened with `pure_eval=True` produces a pure
  evaluator from a bare `session.eval(store)`, and a per-call
  `eval_settings=NixEvalSettings(pure_eval=False)` overrides it.
* **Tests:** two cases per scope — default applied, override wins — on both
  engines.

**0.3 — Move the construction-time guard into `CoreEvalState.configure`**

* **Scope:** Add the two `reject_construction_time_keys` calls to
  `CoreEvalState.configure`. Delete `NixCore.configure_eval_state`, the
  `EvalSettingsTarget` protocol, and the static-assertion helper in its unit
  test.
* **Files:** `_core/_objects.py`, `_core/_nix_core.py`,
  `tests/nanopynix/core/test_nix_core_unit.py`.
* **Depends on:** nothing.
* **Benefit:** the documented protocol guarantee becomes real.
* **Risk:** low.
* **Size:** S
* **Acceptance:** a hand-built `ConfigureEvalRequest` carrying `pure-eval` is
  refused by the worker.
* **Tests:** rewrite the unit test to use a real construction-time key; add an
  rpc test that builds the request directly, as `scratchpad/probe6.py` does.

**0.4 — Let cancellation through `inproc.Session.close`**

* **Scope:** Catch `Exception` in `close_resource` (`:328`) and in the
  `executor.shutdown` handler (`:345`), not `BaseException`. Leave the three
  cleanup-and-reraise handlers at `:267`, `:981` and `:1227` alone.
* **Files:** `inproc/_impl.py`.
* **Depends on:** nothing.
* **Benefit:** structured concurrency works around an inproc close.
* **Risk:** low. rpc has run this way since its own fix.
* **Size:** S
* **Acceptance:** `CancelledError` propagates out of a cancelled `close()`,
  and `anyio.fail_after` around `close()` raises `TimeoutError`.
* **Tests:** two, beside rpc's existing
  `test_a_cancelled_close_still_stops_the_worker_process`.

**0.5 — Restore `NIX_USER_CONF_FILES` on inproc close**

* **Scope:** Save and restore, or document the permanence.
* **Files:** `inproc/_impl.py`.
* **Size:** S
* **Acceptance:** the variable holds its pre-session value after
  `Session.close()`.

### Phase 1 — Boundary and lifecycle hardening

---

**1.1 — Release an inproc value when its Python handle is collected**

* **Scope:** Weak value set on `CoreEvalState`; `weakref.finalize` on
  `inproc.Value` that queues rather than calling Nix; drain at the top of
  `EvalSession.run`.
* **Files:** `_core/_objects.py`, `inproc/_impl.py`.
* **Depends on:** 0.4 (both touch close ordering).
* **Benefit:** removes an unbounded native-memory growth in the engine
  `pynix`'s REPL and LSP use.
* **Risk:** medium. Finalizers run on arbitrary threads; the queue is what
  keeps that safe. Copy rpc's `_DeferredReleases` shape rather than inventing
  one.
* **Size:** M
* **Acceptance:** `probe9.py`'s count returns to 1 after drop, collect, and
  one evaluator operation.
* **Tests:** a resource test with explicit numbers; plus a test that a value
  still referenced by the caller survives a collection and remains usable.

**1.2 — Kill the worker, three ways**

* **Scope:** Add a test-only accessor for `WorkerClient._worker_proc`. Three
  tests: kill mid-call; kill with handles outstanding then close; reopen a
  fresh session afterwards.
* **Files:** `tests/nanopynix/rpc/client/test_workerpool.py`,
  `rpc/client/_pool.py` (accessor only).
* **Depends on:** F6's `EngineError` if the tests assert on the new base;
  otherwise nothing.
* **Benefit:** the engine's stated reason to exist becomes tested.
* **Risk:** low for the library, medium for suite flakiness. Bound each test
  with `anyio.fail_after` and assert classes, not messages.
* **Size:** M
* **Acceptance:** all three pass on both backends, and repeatedly under the
  `concurrency` marker.

**1.3 — Bound the log stall and count what is dropped**

* **Scope:** Bounded wait in `LogCollector.callback` with a `dropped` counter
  reported by `stats()`; `max_events` on `LogCapture` with a `truncated` flag.
* **Files:** `logging.py`, `rpc/worker/_worker.py` (diagnostics dump).
* **Depends on:** nothing.
* **Benefit:** a stalled consumer becomes a visible, bounded degradation
  instead of a hang.
* **Risk:** low. The default timeout must be generous.
* **Size:** M
* **Acceptance:** an operation emitting more than `maxsize` events into a
  consumer that never drains completes, and `stats()["dropped"] > 0`.

### Phase 2 — API and type-system improvements

---

**2.1 — Give the worker a typed state and a typed handle registry**

* **Scope:** Move `WorkerState` to `rpc/worker/_state.py`. Type all three
  handlers' `state` parameter. Replace `get_typed` with four typed accessors.
  Delete `HandleRegistry.clear()`.
* **Files:** `rpc/worker/_state.py` (new), `_worker.py`, `_worker_eval.py`,
  `_worker_store.py`, `_handle_registry.py`.
* **Depends on:** Phase 0 and 1 (do not refactor under an unfixed defect).
* **Benefit:** the RPC handler layer becomes type-checked for the first time.
* **Risk:** low; mechanical, and pyright reports the mistakes.
* **Size:** M
* **Acceptance:** `pyright` stays at zero, and the suppression count in
  `rpc/worker/_worker.py` drops from 16 to fewer than 6. Count both.

**2.2 — One coherent public surface**

* **Scope:** Complete both `__all__` lists. Add `EngineError` and move
  `WorkerDiedError` under it. Type `Session.subscribe`. Delete `Nix`. Decide
  and act on `inproc.Store.call`.
* **Files:** `nanopynix/__init__.py`, `exceptions.py`, `rpc/__init__.py`,
  `rpc/client/session.py`, `rpc/client/_pool.py`, `inproc/_impl.py`,
  `docs/nanopynix/api/*.md`.
* **Depends on:** nothing, but land after Phase 0 so the settings names are
  settled.
* **Benefit:** a documented surface a caller can rely on, and a taxonomy with
  no orphan.
* **Risk:** low. Deleting `Nix` touches one test.
* **Size:** M
* **Acceptance:** the new `test_public_surface.py` passes; every `__all__`
  entry has a documentation page.

**2.3 — Retire the untyped escape hatches**

* **Scope:** `CoreEvalState.__getattr__` — replace the remaining unlisted
  calls with typed methods, then delete it. Its docstring already says the
  `CoreStore` equivalent has gone.
* **Files:** `_core/_objects.py`, `inproc/_impl.py`, `rpc/worker/_worker_eval.py`.
* **Depends on:** 2.1.
* **Benefit:** the last `Any` hole in the shared core closes.
* **Risk:** medium. Find every call first: the forwarded names include
  `repl_active`, `begin_repl`, `repl_scope_names` and `reset_file_cache`.
* **Size:** M
* **Acceptance:** `_core/_objects.py` has no `__getattr__`, and pyright stays
  at zero.

### Phase 3 — Test architecture

---

**3.1 — A settings routing matrix**

Five scopes × two engines × two backends, asserting the value through the door
that owns it. This is the test that would have caught F1. **Size: M.**

**3.2 — Property-based round trips for `stores.py`**

`tests/nanopynix/test_stores.py` covers the models by hand, case by case.
Replace the repetitive part with a property: `parse(uri) → model → uri()` must
be stable for every model, and
`model → uri() → parse()` must return an equal model. Hypothesis strategies
built from `model_fields` cover the 49 settings without 49 hand-written cases.
The S3 and SSH stores cannot be opened locally, so state plainly that they are
round-trip tested and not opened. **Size: M.**

**3.3 — Serialization round trips for the wire types**

Every message in `common.proto` that carries a Nix concept, encoded and
decoded, including the recursive `DerivationOutputs` tree and the trimmed
`NixErrorInfo`. `_status_details.py`'s budget trimming deserves a test with a
1000-frame trace. **Size: S.**

**3.4 — Hostile boundary conditions**

The empty string is already handled well (`_require_filesystem_path`). Extend
to: a store URI with an unknown scheme; a URI parameter the model rejects; a
`NixSettings` field with a value Nix rejects; a handle number the worker never
issued; a `ValueProxy` used after its session closed *and* after the worker
died. **Size: M.**

**3.5 — Unsupported Nix versions**

The per-version dev shells exist. Make the drift check and the version-gated
settings tests run under 2.31 and 2.35 in CI, not only by hand. **Size: S**
(depends on 4.1).

### Phase 4 — Tooling and documentation

---

**4.1 — A `checks` flake output, wired into CI**

`lint`, `lint-strict`, `format`, `types`, `drift`. One fast CI job builds
them. See F13. **Size: M.**

**4.2 — Execute the documentation**

Move every runnable Markdown block into `docs/examples/`, include it by
literal reference, and fix `README.md` and `docs/nanopynix/api/settings.md`.
**Size: M. Depends on 0.1** — the settings example only becomes correct after
it.

**4.3 — Write the ownership rules down**

The five rules in section 4 belong in `_core/_objects.py`'s module docstring
and in `docs/nanopynix/architecture.md`. **Size: S.**

**4.4 — Resolve the stdio worker**

Give it a client and a test, or delete it and the console script. **Size: S.**

### What this roadmap changes for an existing caller

Checked item by item, for both engines. Most items only make a failing call
succeed, which breaks nothing.

| Item | Breaks a working caller? |
|---|---|
| 0.1 routing | No. A global-scope field still reaches `globalConfig`. The four other scopes move from raising to working. |
| 0.2 session defaults | No. A field set on the call always wins over the session default, so every existing call keeps its behaviour. |
| 0.3 worker guard | **Only a caller that bypasses the public API.** A hand-built `ConfigureEvalRequest` carrying a construction-time key stops being accepted. That is the point of the item. `configure()` itself is unchanged: it already refuses. |
| 0.4 cancellation | **Yes, for inproc, deliberately.** A cancelled `close()` now raises `CancelledError` instead of a `BaseExceptionGroup`. A caller catching the group loses its match. This aligns inproc with rpc, which already behaves this way. |
| 0.5 environment restore | **Yes, for inproc.** A caller relying on `NIX_USER_CONF_FILES` surviving `Session.close()` loses it. No such caller exists in this repository or in `pynix`. |
| 1.1 value release | No. A value the caller still references is still reachable, so the finalizer does not run. |
| 1.2, 1.3, 3.x, 4.1, 4.3 | No. Tests, counters and tooling. |
| 2.1 typed worker state | No. Internal to the worker. |
| 2.2 surface | **Yes, in one place:** deleting `Nix = Session`. `ROADMAP.md` states there is no compatibility promise before 1.0. Everything else in 2.2 adds names or adds types. |
| 2.3 escape hatches | **Yes, for a caller using `CoreEvalState.__getattr__`.** That is a private class in a private module. |

The four deliberate breaks are 0.4, 0.5, 2.2's alias removal and 2.3. Each is
listed in its own acceptance criteria. Nothing else changes an API that works
today.

---

## 6. Proposed quality gates

### Every commit — fast, hermetic, no Nix daemon needed

```
direnv exec . ruff format --check
direnv exec . ruff check
direnv exec . ruff check --config ruff-strict.toml
direnv exec . pyright
direnv exec . pytest tests/nanopynix/test_settings.py tests/nanopynix/test_stores.py \
                     tests/nanopynix/test_models.py tests/nanopynix/test_exceptions_classify.py \
                     tests/nanopynix/test_protocols.py tests/nanopynix/test_engine_parity.py
```

Policy: all four static gates report zero. `ruff-strict.toml` stays at zero
findings; a new finding comes from the change under review. Never pass
`--unsafe-fixes`. Never run `treefmt` as a check — it writes.

### Every pull request — the correctness gate

```
direnv exec . timeout 1500 pytest tests --nix-test-backends local,daemon
```

Policy: no failures, no new skips. A skip that is new must name the capability
it needs, through an existing marker (`nix_version`, `nix_capability`,
`nix_known_issue`).

### Every pull request — the drift gate (new)

```
nix build --file . checks.drift
```

Runs `check_all_settings_model_drift(include_optional=True)` and
`check_all_store_model_drift()` under each supported Nix. Policy: `missing` and
`extra` are both empty for every surface and every store model. This is what
catches a Nix release adding or renaming a setting.

### Merge to the default branch — the expensive matrix

* `--nix-test-backends local,daemon` under Nix 2.31, 2.34 and 2.35.
* The ThreadSanitizer jobs, over the `concurrency` and `tsan_stress` markers.
* Coverage, with the forkserver instrumentation already configured in
  `pyproject.toml`.

Policy on coverage: use it to find untested *modules*, not to hold a
percentage. The useful boundary here is per-file: a file under 60 % is worth a
look, and the whole-project number is not worth a gate.

### Platform-specific, opt-in

* The namespace tests, which need unprivileged user namespaces and a
  filesystem with user extended attributes. `probe_namespace_support` already
  reports this; keep the skip keyed to it.
* The `benchmark` marker stays opt-in and is never a correctness gate.

### What deliberately stays out

* A second type checker. `pyright --strict` plus beartype at runtime is
  sufficient coverage of the same question, and mypy would need its own
  suppression vocabulary in 142 places.
* A separate import linter. The dependency rules in section 4 are three lines
  and are better as a test than as a tool.
* Any tool that writes in CI.

---

## 7. Deferred or rejected ideas

**Split `inproc/_impl.py` before the interfaces exist.** Rejected for now.
Splitting turns 43 same-module private accesses into 43 cross-module ones,
which is worse. See F12: introduce the interfaces first, split second.

**Replace `getattr`-based RPC proxying with generated code.** Deferred.
`RpcProxyMixin` and `worker_op` already collapse the two-method-per-RPC
pattern, and the generated betterproto2 bases give the message types. The
remaining `getattr(self._worker.eval_stub, method_name)` is one line and is
covered by the proxy's own tests. Revisit only if the RPC count grows again.

**A `WorkerBusyError` for overlapping evaluator calls** (`ROADMAP.md` item 1).
Deferred, and possibly obsolete. `EvalProxy._operation_lock` already serialises
evaluator calls, and store calls already overlap. The ROADMAP item was written
when the two shared one lane. Confirm the current behaviour is what the
maintainer wants before adding an exception for it.

**Remove the `capture=True` return unions** (`ROADMAP.md` item 3). Already
done, as far as this review can see: `LogCapture` is the context-manager form
the item asks for, and no store or eval method returns a union with a capture.
Delete the ROADMAP item rather than implementing it.

**A "pure Python" model layer beside the betterproto2 messages.** Rejected.
The wire type is the domain type here, and `_status_details.py`'s reasoning
about `info` staying a `dict` shows the cost of a parallel representation.

**Pin dependency versions in `pyproject.toml`.** Deferred, and it is an open
question (section 8). Nix pins everything today, and a PyPI-facing bound would
be a second source of truth that nothing checks.

**Add a protocol version to `InitRequest`.** Deferred. The client spawns the
worker from the same installed package, so skew cannot happen. It becomes
necessary only if the stdio entry point gains a client. Record the reasoning in
`worker.proto` so the next reader does not have to re-derive it.

**Change the store executor from `anyio.to_thread` to a dedicated pool.**
Rejected. Nix stores are thread-safe, the four-slot `CapacityLimiter` bounds
the work, and the thread-local logger request id makes correlation correct.
There is no problem to solve.

---

## 8. Open questions

**Q1 — Should a `NixSettings` field outside the global scope be a session
default, or an error?**
This decides the shape of Phase 0. A router (recommended) keeps the catch-all
the design chose and makes `pure_eval` settable once per session. Rejection is
half the work and leaves callers repeating `eval_settings=` at every call.
*Recommended default: the router.*

**Q2 — May `_core` import `nanopynix.settings`?**
It already does, for `SettingsProvenance` and for the rejection helpers, and
Finding 2's fix adds one more use. The alternative is to pass the guard in as a
callback, which is indirection for a rule that is genuinely a property of Nix.
*Recommended default: yes, and say so in the layering rules.*

**Q3 — Is lossless log delivery worth an unbounded stall of the evaluator?**
Finding 11's fix trades one for the other. A build that emits more than 10 000
events into a stalled consumer currently stops.
*Recommended default: bound the wait generously, drop, and count.*

**Q4 — Does the stdio worker have a future?**
It has an entry point, no client, a stale docstring and no test. Keeping it
means giving it a protocol version and a compatibility rule (Q5).
*Recommended default: delete it; add it back with a client when one exists.*

**Q5 — Is the RPC protocol a public interface?**
`common.proto`'s comments are written for an outside reader ("readable by any
language's gRPC tooling"). If that is the intent, the protocol needs a version
field, a compatibility policy and a changelog. If it is internal, say so at the
top of each `.proto`.
*Recommended default: internal for now, stated explicitly.*

**Q6 — Should `nanopynix` carry dependency bounds for PyPI?**
Nix pins everything the project builds and tests against, so bounds would be
unverified claims. But `pydantic`, `betterproto2` and `grpclib` are all
libraries where a major release breaks callers.
*Recommended default: add lower bounds only, for the four libraries whose APIs
the code actually uses; leave upper bounds to Nix.*

**Q7 — How much of `inproc`'s cross-class private access is worth removing?**
Finding 12 proposes interfaces and a split. The smaller step — adopting rpc's
`claim_eval`/`release_eval` spelling — removes the largest group for a fraction
of the cost.
*Recommended default: do the smaller step in Phase 2, and decide on the split
after seeing how much is left.*

---

## Appendix — what was run

| Command | Result |
|---|---|
| `direnv exec . ruff check` | All checks passed |
| `direnv exec . ruff check --config ruff-strict.toml` | All checks passed |
| `direnv exec . ruff format --check` | 259 files already formatted |
| `direnv exec . pyright` | 0 errors, 0 warnings, 0 informations |
| `direnv exec . pytest tests --nix-test-backends local,daemon` | 2534 passed, 0 failed, 0 error, 3 skipped, in 691.5 s (run 0861) |
| `probe1.py` … `probe10.py` | the measurements quoted in section 3 |

The suite is green. The three skips are version-gated or capability-gated and
are correct: `test_daemon_protocol.py::test_process_connection_serves_nix_daemon_protocol`,
`test_python_store_path_info.py::…::test_a_build_without_dispatch_warns_when_a_store_implements_it`,
and `test_util_bindings.py::test_unsupported_primop_registration_fails_early`.

**No finding in this report comes from a failing test.** Every confirmed
defect is a behaviour the suite does not exercise. That is itself the report's
main conclusion about the test strategy: the checks that exist all pass, and
the gaps are where the checks are absent.

The probes are in the session scratchpad at
`/tmp/claude-1000/-home-lillecarl-Code-nanopynix/7de8372c-843c-412f-8b76-41577b36dbfc/scratchpad/`.
They are throwaway measurement scripts, not tests. Section 5 says which of them
should become tests.
