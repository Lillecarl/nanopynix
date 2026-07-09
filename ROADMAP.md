# nanopynix robustness roadmap

This roadmap captures the next protocol/API work that should make nanopynix
more robust, easier to type-check, and easier to understand. Backwards
compatibility is not a priority while the API is still settling.

## 1. Make worker calls explicitly serial

The worker is effectively a single Nix thread hosted in a subprocess. The API
should expose that reality instead of silently queueing calls.

- Add a `WorkerBusyError` exception.
- If a store call or eval proxy call starts while the worker is already held,
  raise `WorkerBusyError` immediately by default.
- If a timeout is supplied, wait for the worker until that timeout is reached,
  then raise `WorkerBusyError`.
- Keep `EvalSession` as an explicit exclusive lease; proxy operations inside it
  still serialize on the reserved worker.

This makes accidental concurrency obvious and avoids implying that one Nix
worker can process multiple calls in parallel.

## 2. Raise from the currently waiting RPC call on fatal Nix log events

Nix `STDERR_ERR` / worker `action == "error"` events mean the current operation
raised. The awaiter for that operation should see the exception at the local
call site.

- Track the active RPC call in `_WorkerManager`.
- Treat request IDs as diagnostic correlation metadata, not as the primary
  routing mechanism for serial workers.
- When a fatal error log event arrives during an active call, convert it to the
  appropriate `NixError` and fail that call's future immediately.
- Keep JSON-RPC `"error"` responses as a fallback for worker/Python/protocol
  exceptions and for cases where no fatal log event was emitted.
- Continue forwarding all log events to subscribers.

This makes `await store.query_path_info(...)` and `await value.force()` behave
like local calls that raise where the failed operation is awaited.

## 3. Remove per-method log capture return unions

The current `capture=True` shape creates `T | Capture[T]` return types, which
adds type-checking friction and makes every API method harder to reason about.

- Remove `capture` from public store/eval method return types.
- Keep normal methods returning `T` or raising.
- Add explicit log subscription/capture helpers, such as a context manager that
  records events while a block runs.
- Optionally attach same-call logs to raised `NixError` instances once active
  call tracking exists.

Logs should be a stream/diagnostic facility, not a flag that changes every
method's result type.

## 4. Keep lazy path builders sync; keep RPC operations async

Value proxy navigation should match Nix laziness. Building a path to a value is
local; forcing or inspecting it crosses the RPC boundary.

- Keep `ValueProxy.attr(...)`, `ValueProxy.list_get(...)`, `ValueAttrs.__getitem__`,
  and `ValueList.__getitem__` synchronous lazy path builders.
- Ensure operations that touch Nix remain async: `force`, `force_deep`, `type`,
  `call`, `attr_names`, `has_attr`, and `list_length`.
- Document that lazy path builders do not validate existence; the forcing or
  inspection operation gets the error.

This keeps common value navigation ergonomic while making actual RPC boundaries
visible.

## 5. Add worker-side importable primop registration

Python primops are attractive for well-scoped extensions such as subnet
calculation and YAML parsing/rendering. They should run in the worker process,
not call back into master during evaluation.

- Add a typed primop spec model: name, arity, arg names, doc, importable callable
  path.
- Send configured primop specs during worker init.
- Import and register the callable in the worker before creating an `EvalState`.
- Fail registration early and clearly if the worker environment lacks the
  callable dependency, such as `pyyaml`.
- Keep in-process `nanopynix.register_primop(...)` for low-level/direct
  `EvalState` use.
- Defer master-callback/RPC primops; they introduce nested RPC and deadlock
  risks.

This preserves the serial worker model and keeps dependency ownership explicit:
worker-executed primops require dependencies in the worker environment.
