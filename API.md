# API — nanopynix v2

*Status: implemented incrementally.  This document is the intended public API;
some raw nanobind bindings still exist for low-level tests and migration work.*

## Principles

1. **Session-centric.**  One `Session` = one subprocess = one EvalState.  No implicit
   pooling.  Users who need throughput open multiple sessions.
2. **Explicit lifecycle.**  Everything that needs cleanup is a context manager.
   `Session`, `Store`, and `Eval` all support both `async with` and
   explicit `open()`/`close()`.
3. **Stateless things are module-level.**  Parsers, models, constants need no
   subprocess and live at `nanopynix.<name>`.
4. **Subscriber-based log bus.**  Worker log events are delivered to an internal
   event bus.  **Zero subscribers → logs are discarded.**  Subscribers:
   - `session.subscribe(callback)` — live callback for CLI printing.
   - `session.log_stream()` — convenience `AsyncIterator` wrapper.
   - Operation log subscriber (internal) — activated when `capture=True`,
     buffers events for that `request_id`, triggers STDERR→exception
     classification, returns in `Capture.logs`, auto-unsubscribes.
5. **Config is simple.**  `dict[str,str]` + `list[str]` experimental features.

## Architectural constraints

- **EvalState singleton.**  Nix is single-threaded per process.  One Session =
  one subprocess = one EvalState.  `session.eval()` blocks a second `eval()`
  on the same session until the first exits.
- **Lifecycle modes.**  Context managers AND manual `open()`/`close()` on
  Session, Store, Eval.

---

## Module-level (no Session needed)

```python
nanopynix.StorePath("abc123-foo")            → StorePath
nanopynix.parse_flake_ref("github:...")       → FlakeRef
nanopynix.yaml_primops()                     → list[PrimOpSpec]
```

Exceptions: `NixError`, `UndefinedVarError`, `TypeError_`, `ThrownError`,
`InfiniteRecursionError`, `AssertionError_`, `MissingArgumentError`,
`RestrictedPathError`, `ParseError`, `StoreError`, `UsageError`.

Nix error text may include ANSI color escapes because Nix formats diagnostics
for CLI output.  `nanopynix.strip_ansi(text)` removes those escapes when callers
want clean text while preserving the raw error payload.  `NixError` exposes
`msg_without_ansi` and `raw_without_ansi`; `LogEvent` exposes `message`,
`message_without_ansi`, and `without_ansi()`.

Pydantic models at `nanopynix.models.*`.

YAML helpers are intended for worker-side primops:

```nix
builtins.fromYAML "... YAML 1.2/core-style document ..."
builtins.fromYAML11 "... YAML 1.1 document, e.g. 0444 as octal ..."
builtins.fromYAMLStream "... multi-document YAML 1.2 stream ..."
builtins.fromYAML11Stream "... multi-document YAML 1.1 stream ..."
builtins.toYAML { kind = "ConfigMap"; }
builtins.toYAML [ { kind = "ConfigMap"; } { kind = "Service"; } ]
```

`toYAML` emits Kubernetes-compatible YAML from JSON-compatible Nix values.  A
root list renders as a multi-document stream; other values render as a single
document.  There is no YAML 1.1 emitter; legacy support is parse-only.
`fromYAML` and `fromYAML11` require exactly one document.  A single document
whose root value is a list stays a list; multi-document input must use the
`*Stream` parser.

```python
@dataclass
class Capture[T]:
    value: T                       # the operation result
    logs: list[LogEvent] | None    # None when capture=False
```

---

## Session

```python
async with nanopynix.Session(
    nix_conf: str | None = "/etc/nix/nix.conf",  # None = skip
    config: dict[str, str] | None = None,          # NIX_CONFIG
    experimental_features: list[str] | None = None,
) as session:
```

Advanced store selection is still available through `store_uri` and
`eval_store_uri`.  `settings=` is accepted as a temporary alias for `config=`.

### Log bus

```python
# Live subscriber
sub = session.subscribe(lambda event: print(event.action))
sub.unsubscribe()

# Convenience iterator
async for event in session.log_stream():
    print(event)
```

Internally: a `_LogBus` with add/remove subscriber.  Worker relay feeds
events to the bus.  Zero subscribers → bus drops events (no buffering).

When `capture=True` on any operation, an internal `_OpLogSubscriber`
subscribes, buffers events matching that `request_id`, classifies STDERR
messages into NixError subclasses, unsubscribes on completion, and
populates `Capture.logs`.

---

## Store

```python
async with session.store(uri: str = "daemon") as store:
```

StoreHandle carries `_session_id` (uuid).  `session.eval(store)` checks
equality at runtime → `ValueError` if mismatched.

Every method accepts `capture: bool = False`.  With `capture=False` it returns
the plain value.  With `capture=True` it returns `Capture[value]` with matching
worker log events.

| Group | Methods |
|-------|---------|
| Identity | `get_uri() → Capture[str]`, `get_store_dir() → Capture[str]` |
| StorePath | `parse_store_path(s) → Capture[StorePath]`, `is_valid_path(sp) → Capture[bool]`, `follow_links_to_store_path(s) → Capture[StorePath]` |
| Path info | `query_path_info(sp) → PathInfo`, `query_path_from_hash_part(h) → StorePath \| None` |
| Closures | `compute_fs_closure(sp, *, flip, include_outputs, include_derivers) → Capture[list[StorePath]]`, `query_missing(paths) → Capture[MissingInfo]` |
| Derivations | `query_derivation_outputs(sp) → Capture[list[StorePath]]`, `query_valid_derivers(sp) → Capture[list[StorePath]]`, `read_derivation(drv) → Capture[Derivation]`, `build_derivation(drv, mode) → Capture[BuildResult]` |
| Bulk | `query_all_valid_paths()`, `query_referrers(sp)`, `query_substitutable_paths(paths)` — all `Capture[list[StorePath]]` |
| Build | `build_paths_with_results(paths) → Capture[list[BuildResult]]` |
| GC | `add_temp_root(sp) → Capture[None]` |
| Fetchers | `fetch_from_url(url) → Capture[Input]`, `fetch_from_attrs(attrs) → Capture[Input]` |

---

## Eval

```python
async with session.eval(store: StoreHandle) as eval_:
```

### Methods

```
eval_.file(path, *, timeout=None, capture=False)     → Capture[ValueProxy]
eval_.string(expr, *, path="<string>",                → Capture[ValueProxy]
             timeout=None, capture=False)
eval_.lock_flake(ref, *, timeout=None, capture=False) → LockedFlake
eval_.get_flake(ref, *, timeout=None, capture=False)  → FlakeRef
```

### Eval value types

The public eval type aliases are exported from `nanopynix.types` and the
package root:

```python
type NixArg = ValueProxy | JsonScalar | list[NixArg] | dict[str, NixArg]
type NixValue = ValueProxy | ValueAttrs | ValueList | JsonValue
type NixDeepValue = ValueProxy | JsonScalar | list[NixDeepValue] | dict[str, NixDeepValue]
```

`NixArg` is what can be copied or passed by handle into a Nix function call.
`NixValue` is the result of shallow forcing. `NixDeepValue` is the result of
recursive forcing; nested functions remain `ValueProxy` objects.

### ValueProxy — lazy, all RPC access is async

| Method | Returns | Notes |
|--------|---------|-------|
| `.attr(name)` | `ValueProxy` | lazy, no RPC until forced/queried |
| `.force()` | `NixValue` | WHNF — outer constructor only |
| `.force_deep()` | `NixDeepValue` | recursive (forceValueDeep); functions stay remote |
| `.force_as(type)` | specific value type | strict Nix type check before forcing |
| `.try_int()`, `.try_str()`, ... | specific value type | readable strict wrappers around `force_as()` |
| `.coerce_int()`, `.coerce_str()`, ... | scalar | explicit Python-side scalar coercions |
| `.call(*args)` | `ValueProxy` | |
| `.get_type()` | `NixType` | async; forces enough to classify the value |

Strict helpers raise `WrongNixTypeError` when the Nix value has the wrong
type. Coercion helpers raise `NixCoercionError` when a scalar cannot be
converted; they do not coerce attrsets, lists, or functions into scalars.

### Handle lifetime & early release

Value handles are C++ nanobind objects protected by Boehm GC.  The unified
rule: **anything that holds a GC handle supports `async with` for early
release.**  This includes `ValueProxy`, `ValueAttrs`, and `ValueList`.
Scalars (int, str, bool) support `async with` as a no-op.

Without `async with`, handles are batch-released when the Eval context
manager exits.  With `async with`, the handle is released immediately on
`__aexit__`, telling the subprocess to free the C++ value so the GC can
collect it.  This matters when evaluating many things consecutively within
one EvalState session — dead values accumulate otherwise.

```python
# Batch release — all handles live until Eval exit
async with session.eval(store) as eval_:
    root = await eval_.file("default.nix")
    attrs = await root.value.force()      # attrs handle created
    name = await attrs["name"].force()    # name handle created
# both handles released here

# Early release — free handles as soon as you're done
async with session.eval(store) as eval_:
    root = await eval_.file("default.nix")
    async with root as r:
        attrs = await r.value.force()
        async with attrs as a:
            async with a["name"] as name_proxy:
                name = await name_proxy.force()
            # name_proxy handle released
        # attrs handle released
    # root handle released
```

The `async with` composes naturally with `__getitem__` and `force()`:

```python
async with attrs["name"] as name_proxy:     # ValueProxy from __getitem__
    ...

async with (await attrs["name"].force()) as forced:
    # forced is ValueAttrs | ValueList | scalar (no-op)
    ...
```

### ValueAttrs — attrset forced to WHNF

```python
attrs = await value.force()          # ValueAttrs — keys accessible, values lazy
list(attrs.keys())                   # ["name", "system", "outputs"]
name_val = attrs["name"]             # ValueProxy — still lazy
name = await name_val.force()        # "hello-2.12.3"
await attrs["name"].force()          # shorthand: force a single key
```

### ValueList — list forced to WHNF

```python
lst = await value.force()            # ValueList — length accessible, elements lazy
len(lst)                             # 3
first = lst[0]                       # ValueProxy
await lst[0].force()                 # force one element
```

---

## Derivation model

```python
class Derivation(BaseModel):
    name: str
    system: str
    builder: str
    args: list[str]
    env: dict[str, str]
    input_drvs: dict[StorePath, DerivationOutputs]
    input_srcs: list[StorePath]

class DerivationOutputs(BaseModel):
    outputs: list[str]
    dynamic_outputs: dict[str, str] = {}
```

Mirrors C++ `nix::Derivation`, not the richer `nix derivation show` JSON.

---

## Full example

```python
async with nanopynix.Session(
    config={"max-jobs": "4"},
    experimental_features=["flakes"],
) as session:
    # Live log printing
    sub = session.subscribe(lambda e: print(f"[{e.action}] {e.args}"))

    async with session.store() as store:
        info = await store.query_path_info(sp, capture=True)
        print(info.value.nar_hash)
        for event in info.logs:
            print(f"  captured: {event.action}")

        async with session.eval(store) as eval_:
            root = await eval_.file("default.nix")
            async with root as r:           # early release
                attrs = await r.value.force()
                async with attrs["name"] as name_proxy:
                    name = await name_proxy.force()
                    print(f"package: {name}")

    sub.unsubscribe()
```

---
## Implementation chunks (proposed order)

### Chunk 1 — Subprocess & protocol
Strip `WorkerPool` down from multi-worker pool to single-worker manager.
Rename `Nix` → `Session`.  Remove `reserve`/`release`, `_acquire`/`_release`,
idle timeout, `_workers` list, concurrent close.  One worker, one pipe pair.

**Deliverable:** `Session` opens one subprocess.  Internal only — no public
API beyond `Session.__aenter__`/`__aexit__`.

### Chunk 2 — Log bus + subscriber
Replace `_log_events` queue with `_LogBus(subscribe/unsubscribe/emit)`.
Zero-op when no subscribers.  `session.subscribe()`, `session.log_stream()`.
Internal `_OpLogSubscriber` for `capture=True` with STDERR→exception.

**Deliverable:** subscriber-based logging, `capture=True` returns logs.

### Chunk 3 — Store context manager
Wrap `Store` facade in context manager.  `_session_id` check.  `open()`/`close()`.
Add `capture=` parameter to all methods.

**Deliverable:** `async with session.store() as store:` works.

### Chunk 4 — Eval + ValueProxy cleanup
Wrap `EvalSession` in context manager.  `eval_file`→`file`, `eval_string`→`string`.
`ValueProxy.__aenter__`/`__aexit__` for early release.  Add `capture=`.

**Deliverable:** `async with session.eval(store) as eval_: root = await eval_.file(...)`.

### Chunk 5 — ValueAttrs / ValueList
Lazy compound types with handle management.  `__getitem__` → `ValueProxy`,
`.keys()`, `__len__`, `__aenter__`/`__aexit__`.  `force_deep()`.

**Deliverable:** lazy attribute access, handles compose with `async with`.

### Chunk 6 — Module-level cleanup
Move stateless things to `nanopynix.*`.  Clean `__init__.py`.  Remove old
`Nix`, `WorkerPool`, `_pool.py` remnants.

**Deliverable:** clean public API surface.

### Chunk 7 — Config, nix_conf, Derivation model
Wire `nix_conf` → `NIX_USER_CONF_FILES`, `config` → `NIX_CONFIG`,
`experimental_features`.  Add `Derivation` + `DerivationOutputs` models.

**Deliverable:** config works, derivation model complete.

### Chunk 8 — Tests
Port all tests to new API.  Add tests for: subscriber bus, `capture=True`,
ValueAttrs/ValueList, early release, session-id check, config, Derivation.

**Deliverable:** test suite passes with new API.
