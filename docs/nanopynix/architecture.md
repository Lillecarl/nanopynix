# Workers vs in-process

nanopynix has two async APIs for running Nix, and they present nearly the
same shape:

- `nanopynix.Session` — runs Nix in an isolated worker **subprocess** and
  talks to it over gRPC. See {doc}`api/session`, {doc}`api/store`,
  {doc}`api/eval`.
- `nanopynix.inproc.Session` — runs Nix directly on one dedicated thread in
  **this process**. No subprocess, no serialization. See {doc}`api/inproc`.

Both wrap the same thread-confined, synchronous L1 bindings underneath
(`nanopynix._local`) — a worker and an in-process session evaluate Nix the
same way internally. What differs is the process boundary around that core,
which shows up as a handful of concrete constraints.

## What's the same

- `session.store(uri)` / `session.eval(store)` context managers.
- Store query methods: `query_path_info`, `compute_fs_closure`,
  `collect_garbage`, `find_roots`/GC roots, and so on.
- Flake locking (`lock_flake`, `eval_flake`), REPL sessions (`repl()`,
  `.line()`, `.load_file()`), settings (`NixSettings`), and verbosity control.
- Values are session-bound and must be released (or used through
  `async with`) before their owning eval session closes.

## What's different

| | `Session` (worker) | `inproc.Session` |
|---|---|---|
| Process model | subprocess, forkserver-based, gRPC | this process, one dedicated Nix thread |
| Concurrent instances | any number, each independently configured | at most **one** open per process |
| Crash isolation | a worker crash/OOM raises `WorkerDiedError` — your process survives | a Nix-side crash takes the whole process down |
| Custom Python primops | yes — `Session(primops=..., primop_callables=...)` | not supported |
| Call overhead | gRPC request/response per call | direct call on the Nix thread |
| Forcing a compound value | `ValueProxy.force()` returns a lazy `ValueAttrs`/`ValueList` view | `Value.force()` converts straight to a Python `dict`/`list` |
| Nix library initialization | scoped to each worker subprocess | process-global — a second `inproc.Session` with different settings raises |

The process-global initialization is the constraint most likely to surprise
you: `inproc.Session.open()` raises if another `inproc.Session` is already
open in this process, and a session owns at most one live `EvalSession` at a
time (one `EvalState` pointer, period). `Session` has no such limit — open as
many independently configured worker subprocesses as you need.

## When to pick which

Reach for **`Session` (worker)** when any of these apply:

- You need more than one independently configured Nix instance at once
  (different `nix.conf` settings, experimental features, or store URIs).
- You're evaluating expressions you don't fully trust, or that could crash
  or hang Nix — the subprocess boundary means that failure doesn't take your
  host process down with it.
- You need custom Python-backed primops.

Reach for **`inproc.Session`** when:

- Your process needs exactly one Nix configuration for its whole lifetime.
- You want the lowest overhead per call and don't need a second, independently
  configured Nix instance in the same process.
- You don't need custom primops.

pynix, nanopynix's own CLI, uses the worker API (`Session`) throughout — see
{doc}`../pynix/index`.
