# nanopynix code improvement plan

Written after a read of the Python sources, the proto schemas, the test tree,
the packaging and the CI definition, plus ten measurement probes against a
running Nix 2.34. Each finding names the file and the symbol it comes from.
Each measurement gives the command and the result.

The prose follows ASD-STE100, as `AGENTS.md` requires.

**This file is an index. It holds no status.** Each finding is a GitHub issue,
and the issue is the record of what is open and what is done. Issue
[#26](https://github.com/Lillecarl/nanopynix/issues/26) tracks the whole
roadmap. The reasoning that is not a work item lives under `docs/nanopynix/`.
Section 3 maps a finding to its issue, and section 5 names the documents.

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

1. **The configuration API raises on four of its five scopes** (F1, [#6](https://github.com/Lillecarl/nanopynix/issues/6)).
   This is the headline object, the documented example fails, and no test
   covers it.
2. **A documented safety guard does not exist in the worker** (F2, [#8](https://github.com/Lillecarl/nanopynix/issues/8)).
   The docstring says the worker re-checks. It does not.
3. **The in-process engine never frees a Nix value** (F3, [#11](https://github.com/Lillecarl/nanopynix/issues/11)). Measured:
   1501 rooted values retained after three calls that a caller believes it
   released.
4. **Crash isolation is the RPC engine's stated reason to exist, and no test
   kills a worker** (F10, [#12](https://github.com/Lillecarl/nanopynix/issues/12)).
5. **No lint, type or format gate runs in CI** (F13, [#22](https://github.com/Lillecarl/nanopynix/issues/22)). Three clean gates
   are maintained by hand.

### The five changes with the highest expected impact

| Change | Finding | Issue | Size |
|---|---|---|---|
| Route each settings scope to the Nix door that accepts it, and make the router the only path | F1 | [#6](https://github.com/Lillecarl/nanopynix/issues/6) | L |
| Give the worker the guard its client already has, and delete the unreachable copy | F2 | [#8](https://github.com/Lillecarl/nanopynix/issues/8) | S |
| Give `inproc.Value` the release-on-collect behaviour `rpc.ValueProxy` already has | F3 | [#11](https://github.com/Lillecarl/nanopynix/issues/11) | M |
| Add a `checks` flake output that runs pyright, both ruff configurations and the drift checks; wire it into CI | F13 | [#22](https://github.com/Lillecarl/nanopynix/issues/22) | M |
| Kill a worker process, in three ways, and assert what the caller sees | F10 | [#12](https://github.com/Lillecarl/nanopynix/issues/12) | M |

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

**This is where the architecture and the model disagree.** See F1,
[#6](https://github.com/Lillecarl/nanopynix/issues/6).

### Value ownership, by engine

| | `inproc.Value` | `rpc.ValueProxy` |
|---|---|---|
| Backing resource | a rooted `CoreValue` on the evaluator thread | an int handle in the worker's `HandleRegistry` |
| Registered with | `CoreEvalState._values`, a strong `set` | `_DeferredReleases`, plus a `weakref.finalize` |
| Freed on explicit release | yes | yes |
| Freed when the Python object is collected | **no** | yes, drained at the next RPC |
| Freed at evaluator close | yes | yes |

---

## 3. Findings, and where each one lives now

Each finding is a GitHub issue. The issue carries the evidence, the scope, the
acceptance criteria and the tests. **This table holds no status**, because the
issue holds the status, and two records of the same fact disagree in time.

Issue #26 tracks the whole roadmap as a checklist.

| # | Finding | Severity | Confidence | Issue |
|---|---|---|---|---|
| F1 | `Session(settings=NixSettings(...))` raises for every field outside the global scope | critical | confirmed | [#6](https://github.com/Lillecarl/nanopynix/issues/6) |
| F2 | The worker does not enforce the construction-time settings guard | high | confirmed | [#8](https://github.com/Lillecarl/nanopynix/issues/8) |
| F3 | inproc never releases a rooted value when the Python handle is collected | high | confirmed | [#11](https://github.com/Lillecarl/nanopynix/issues/11) |
| F4 | `inproc.Session.close()` swallows cancellation | medium | confirmed | [#9](https://github.com/Lillecarl/nanopynix/issues/9) |
| F5 | Documentation code is never executed; two examples do not run | medium | confirmed | [#23](https://github.com/Lillecarl/nanopynix/issues/23) |
| F6 | `WorkerDiedError` sits outside the taxonomy and outside the surface | medium | confirmed | [#15](https://github.com/Lillecarl/nanopynix/issues/15) |
| F7 | The worker layer is `Any` to the type checker | medium | confirmed | [#14](https://github.com/Lillecarl/nanopynix/issues/14) |
| F8 | Session-scoped defaults exist for stores and for nothing else | medium | confirmed | [#7](https://github.com/Lillecarl/nanopynix/issues/7) |
| F9 | The public surface disagrees with itself | medium | confirmed | [#15](https://github.com/Lillecarl/nanopynix/issues/15), [#16](https://github.com/Lillecarl/nanopynix/issues/16) |
| F10 | Worker-death coverage never kills a worker | medium | confirmed | [#12](https://github.com/Lillecarl/nanopynix/issues/12) |
| F11 | A stalled log consumer stalls Nix, with no bound and no signal | medium | likely | [#13](https://github.com/Lillecarl/nanopynix/issues/13) |
| F12 | inproc has no internal interface; 43 cross-class private accesses | medium | measured; remedy speculative | none — see Q7 in [decisions](docs/nanopynix/decisions.md) |
| F13 | No lint, type or format gate runs in CI | medium | confirmed | [#22](https://github.com/Lillecarl/nanopynix/issues/22) |
| F14 | Dead and stale surface: the stdio worker, `HandleRegistry.clear()`, `Nix` | low | confirmed | [#25](https://github.com/Lillecarl/nanopynix/issues/25), [#14](https://github.com/Lillecarl/nanopynix/issues/14) |
| F15 | `NIX_USER_CONF_FILES` is set and never restored | low | confirmed | [#10](https://github.com/Lillecarl/nanopynix/issues/10) |
| F16 | `nix_type_from_string` is patched onto a generated enum | low | confirmed | folded into [#15](https://github.com/Lillecarl/nanopynix/issues/15) |

Two findings have no issue on purpose. **F12** proposes a refactor whose remedy
this review is not confident about, so it stays an open question rather than a
work item. **F16** is not worth a change of its own, so it rides with the
public-surface work.

## 4. The roadmap

Issue [#26](https://github.com/Lillecarl/nanopynix/issues/26) holds the
roadmap as a checklist of five phases. The order matters: Phase 0 and Phase 1
are the defects, and #14 and #16 refactor the layer that #8 and #11 correct.

| Phase | Issues |
|---|---|
| 0 — correctness and safety | #6, #7, #8, #9, #10 |
| 1 — lifecycle hardening | #11, #12, #13 |
| 2 — API and type safety | #14, #15, #16 |
| 3 — test architecture | #17, #18, #19, #20, #21 |
| 4 — tooling and documentation | #22, #23, #24, #25 |

### What the roadmap changes for an existing caller

Most items only make a failing call succeed, which breaks nothing. Four breaks
are deliberate:

| Item | The break |
|---|---|
| #9 | A cancelled inproc `close()` raises `CancelledError` instead of a `BaseExceptionGroup`. This aligns inproc with rpc. |
| #10 | `NIX_USER_CONF_FILES` no longer survives an inproc `Session.close()`. |
| #15 | `Nix = Session` goes away. `ROADMAP.md` states there is no compatibility promise before 1.0. |
| #16 | `CoreEvalState.__getattr__` goes away. It is a private class in a private module. |

#8 also stops accepting a hand-built `ConfigureEvalRequest` that carries a
construction-time key. That is the point of the issue, and `configure()` itself
does not change.

Nothing else changes an API that works today.

## 5. The reasoning behind the roadmap

Four parts of the review are not work items, so they are living documents
rather than issue bodies:

| Document | What it holds |
|---|---|
| [`docs/nanopynix/architecture-principles.md`](docs/nanopynix/architecture-principles.md) | Layer boundaries, dependency direction, public versus internal, ownership of a native resource, process ownership, the error taxonomy, the behavioural contract between the engines, the sync and async policy, and the compatibility policy. |
| [`docs/nanopynix/quality-gates.md`](docs/nanopynix/quality-gates.md) | The exact command for each gate, and when each gate runs. |
| [`docs/nanopynix/decisions.md`](docs/nanopynix/decisions.md) | The ideas this review rejected or deferred, with the reason for each. The seven open questions, each with a recommended default. |

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
