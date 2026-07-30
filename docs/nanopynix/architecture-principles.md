# Architecture principles

This page states the rules that the layers of nanopynix follow. Where the code
does not follow a rule, the difference is work rather than taste, and an issue
records it.

A code review of 2026-07 wrote these rules down for the first time. `#24`
tracks the remaining work to put the rules for a native resource into the
module docstring of `_core/_objects.py`.


```
bindings  →  _core  →  {settings, stores, models, exceptions, protocols, logging}  →  {inproc, rpc}
```

Dependencies point right to left only. Two rules:

1. **`_core` never imports an engine.** Holds today; keep it.
2. **An engine never imports the other engine.** Holds today; keep it.

One exception is permanent and must stay documented at the import site:
`rpc/client/_pool.py` imports `rpc/worker/_worker.worker_service_factory`,
because the forkserver pickles it by module path.

## Public versus internal

* `nanopynix.__all__` is the public surface. Every entry has a page under
  `docs/nanopynix/api/`. Every public name importable from `nanopynix` is in
  `__all__`. A test enforces both directions (#15).
* `nanopynix.rpc` and `nanopynix.inproc` are the two engine surfaces, and they
  are symmetric by the rule the parity ledger already states: **process
  isolation is the only thing rpc has that inproc does not, so an asymmetry is
  a defect unless process isolation forces it.**
* A module named with a leading underscore is internal. `pynix` may depend on
  one only with a comment at the import site saying why, as `AGENTS.md`
  requires.

## Native resource ownership

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
   backstop. Today rpc obeys this and inproc does not (#11).
5. **A finalizer never calls Nix.** It queues, and the next operation on the
   owning thread drains the queue. rpc does this; inproc should.

## Process ownership

* One `Session` owns one worker process. `WorkerClient` owns its lifetime and
  is the only thing that may stop it.
* Teardown is always reached. The polite half (`Shutdown`) is cancellable; the
  teardown half runs under a shield and is separately bounded. This is already
  correct in `WorkerClient.close` and is the pattern to copy.
* A worker that outlives its teardown is terminated and then killed
  (`_stop_worker_process`). That path is correct and untested (#12).

## Error taxonomy

Four families, and every exception belongs to exactly one:

| Family | Base | Meaning |
|---|---|---|
| Nix said no | `NixError` | Nix was consulted and reported a failure |
| You misused an object | `ObjectMisuseError` | nanopynix refused before reaching Nix |
| The engine failed | `EngineError` *(new, #15)* | the transport or the worker failed |
| Python's own | `TimeoutError`, `ValueError`, … | ordinary Python conditions |

`NixError` keeps its three boundaries (A: nanobind type name, B: the status
trailer, C: daemon prose). That model is sound and documented; leave it.

Boundary B carries the class as a field, not as prose. The worker sends a
`nix.common.ErrorIdentity` beside the `NixErrorInfo`, and the client resolves
it against an allowlist that `nanopynix.exceptions` builds at import. That is
what makes the "same exception class" rule below hold for an exception Nix did
not raise, and it is what lets a client in another language read the class
without splitting a string. Two rules go with it:

- **The identity seeds the resolution; it does not end it.** Nix reports both
  a type error and a hash mismatch as plain `nix::EvalError`, so the client
  still narrows by message. Narrow only, never widen.
- **The identity is never trimmed.** It is a few dozen bytes, and the byte
  budget binds exactly when the trace is deep, which is when the class matters
  most.

The worker keeps the older `"TypeName: message"` prefix on the status message.
It is what a peer without the codec still recovers, and it is why an absent
identity degrades rather than fails.

## Logging must not hold Nix back

> A log event may be lost. Nix's progress may not be delayed. Exactly one hop
> in each process is lossy, it sits at the process boundary, and every hop
> above it is guaranteed to drain. A control event is never lost.

A *control event* is a `request_finalized` marker or the stream sentinel. A log
line is diagnostic and a marker is protocol, which is why a full buffer treats
the two differently: a control event takes the place of the oldest log line
rather than joining the drop count.

Three rules follow, and each one was a defect before #13.

* **No buffer between Nix and a consumer may be unbounded, and none may block
  the Nix thread.** `LogCollector.callback` runs on that thread and returns
  whatever the consumer is doing.
* **Every hop above the lossy one drains unconditionally.** The worker's relay
  task exists only for this: it moves events out of the collector into
  `LogOutbox`, so an HTTP/2 send that parks backs up into a buffer that
  discards rather than into a queue that stops Nix.
* **A cap discards the oldest, and says that it did.** The end of a log is the
  part a reader wants. `LogCapture` reports `truncated` for its own cap and
  `dropped_events` for what a producer threw away, and the producer sends that
  count as a `nix.common.EventsDropped` event so a caller in any language can
  tell that its log is short.

## Transport-independent domain models

`nanopynix.models` re-exports the betterproto2 messages as the canonical data
types, and both engines produce the same objects. Keep it. Do not introduce a
second, "pure Python" model layer beside them; the wire type *is* the domain
type here, and the one place that needed more (`LogEvent`) is handled by a
subclass rather than by a parallel hierarchy.

## The in-process and RPC behavioural contract

The two engines must agree on: exception class, exception message where Nix
authors it, `NixError.info` contents, value laziness, settings acceptance and
refusal, and resource release timing. They may differ on: timeouts (rpc only —
an in-process call has no such failure mode), crash isolation, custom primops,
and the number of concurrently open sessions per process.

`test_engine_parity.py` checks names. `test_engine_parity_semantics.py` checks
behaviour. Both must keep growing; #11 and #6 are each a semantics entry that
was missing.

## Sync and async policy

* Public API is async on both engines. `_core` is synchronous and thread-
  confined. That split is correct and should not move.
* Inside async code, use the anyio primitive. The two documented exceptions
  (`asyncio.wrap_future` in `_nix_executor.py`, and `asyncio.create_task` for
  a portal or scope whose entry and exit are in different tasks) stay, with
  their reasons at the call site.
* A lazy value selector is synchronous on both engines
  (`attr`, `list_get`). Everything that reaches Nix is a coroutine.

## Compatibility and versioning

* **Python API:** no compatibility promise before 1.0, per `ROADMAP.md`. State
  that in the README rather than keeping aliases such as `Nix`.
* **Nix versions:** one model per surface, gated per field with
  `nix_version_min` / `nix_version_removed`, checked by the drift check under
  each supported version's dev shell. This works; make it a CI gate (#22).
* **RPC protocol:** the worker is spawned by the client from the same
  installed package, so version skew cannot occur today. If the stdio entry
  point ever gains a client (#25), the protocol needs a version field in
  `InitRequest` and a compatibility rule. Until then, say so in
  `worker.proto`.

