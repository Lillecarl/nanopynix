# Handler / Executor / Transport Split

## Motivation

Currently `pynixd/operations/*.py` mixes three concerns in one class:

| Concern | Example (IsValidPath) | Lives in |
|---|---|---|
| Wire format | `serialize(ctx)`, `deserialize(ctx)` | `pynixd/serde/` |
| Server dispatch | `handle(ctx)` — auth, routing, stderr framing | `operations/` |
| Fast-path logic | `execute(store)` — SQLite cache, local store | `operations/` |

The serde types have been extracted.  The remaining two concerns should
be split into a **Handler** (server dispatch) and an **Executor**
(fast-path).  This makes the protocol reusable, the dispatch loop
trivial, and fast-path logic testable in isolation.

## Dispatch hierarchy

```
Client message arrives (op code already read)
│
├─ Handler registered? ──Yes──▶ handler.handle(ctx)
│                               ├─ auth check
│                               ├─ try executor fast-path
│                               ├─ fall through to daemon if needed
│                               └─ return response
│
└─ No handler ──▶ Executor registered? ──Yes──▶ executor.execute(req)
│                 │                              (pure query fast-path,
│                 │                               no auth/routing needed)
│                 │
│                 └─ No ──▶ Forward raw bytes to daemon
```

### Definitions

**Handler** — owns the *decision*.  Handles a client message end-to-end:
  - `deserialize` request
  - check auth / rate limiting
  - if a local executor can satisfy the request, call it
  - if not, forward to daemon (or scheduler, for build ops)
  - build response, handle stderr framing

**Executor** — owns a *fast-path implementation*.  Stateless per call:
  - "Can you do this cheaper than a daemon round-trip?"
  - Yes → return `WireResponse` (or the serde response type)
  - No → return `None` (signal to fall through)

**Transport** — the dispatch loop itself.  Reads op code, looks up handler
or executor, falls through to daemon forwarding.  Lives in `pynixd/store/`
or a new `pynixd/dispatch.py`.

## Registration

Both handlers and executors self-register via `__init_subclass__` at
import time, using the `op` number as key.

```python
# handlers/base.py
HANDLER_REGISTRY: dict[int, type[Handler]] = {}

class Handler(ABC):
    op: ClassVar[int]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "op" in cls.__dict__:  # only direct subclasses
            HANDLER_REGISTRY[cls.op] = cls

    @abstractmethod
    async def handle(self, ctx: RequestContext) -> WireResponse: ...


# executors/base.py
EXECUTOR_REGISTRY: dict[int, type[Executor]] = {}

class Executor(ABC):
    op: ClassVar[int]

    def __init_subclass__(cls, **kwargs):
        ...

    @abstractmethod
    def can_handle(self, store: Store) -> bool: ...
    @abstractmethod
    async def execute(self, req: WireRequest, store: Store) -> WireResponse | None: ...
```

## Example: IsValidPath (op 1)

```python
# handlers/is_valid_path.py
class IsValidPathHandler(Handler):
    op: ClassVar[int] = 1
    request_type = SerdeIsValidPathRequest

    async def handle(self, ctx: RequestContext) -> WireResponse:
        req = await SerdeIsValidPathRequest.from_reader(ctx.read_ctx)

        # Try local executor
        if (executor_cls := EXECUTOR_REGISTRY.get(self.op)):
            executor = executor_cls()
            if result := await executor.execute(req, ctx.proxy.local_store):
                return result

        # Forward to daemon
        return await ctx.proxy.local_store.forward(req)


# executors/is_valid_path.py
class IsValidPathExecutor(Executor):
    op: ClassVar[int] = 1

    def can_handle(self, store: Store) -> bool:
        return store.db is not None

    async def execute(self, req: WireRequest, store: Store) -> WireResponse | None:
        if store.tracker.has_path(req.path):
            return SerdeIsValidPathResponse(valid=True)
        # SQLite lookup
        async with store.db.execute(IS_VALID_PATH, (str(req.path),)) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            store.tracker.add_known_path(req.path)
            return SerdeIsValidPathResponse(valid=True)
        return None  # fall through to daemon
```

## Example: QueryPathInfo (op 26)

Handler needed — has auth implications, constructs ValidPathInfo from
SQL rows.  Executor does the heavy SQL:

```python
# handlers/query_path_info.py
class QueryPathInfoHandler(Handler):
    op: ClassVar[int] = 26

    async def handle(self, ctx: RequestContext) -> WireResponse:
        req = await SerdeQueryPathInfoRequest.from_reader(ctx.read_ctx)
        if executor_cls := EXECUTOR_REGISTRY.get(self.op):
            if result := await executor_cls().execute(req, store):
                return result
        return await store.forward(req)


# executors/query_path_info.py
class QueryPathInfoExecutor(Executor):
    op: ClassVar[int] = 26

    def can_handle(self, store: Store) -> bool:
        return store.db is not None

    async def execute(self, req: WireRequest, store: Store) -> WireResponse | None:
        cached = store.get_path_info(req.path)
        if cached is not None:
            return SerdeQueryPathInfoResponse(
                valid=True, info=cached.info
            )
        # Complex SQL with JOINs...
        ...
        return None
```

## Operations that only need executors

Some operations have no handler — pure SQLite fast-path, no auth needed.
If the executor returns `None`, the transport loop falls through to
daemon forwarding automatically.

| Op | Name | Executor | Handler |
|---|---|---|---|
| 1 | IsValidPath | SQLite | Yes (auth, scheduler routing) |
| 6 | QueryReferrers | SQLite | No — pure forward |
| 10 | EnsurePath | SQLite | No — pure forward |
| 23 | QueryAllValidPaths | SQLite | No — pure forward |
| 26 | QueryPathInfo | SQLite | Yes (complex compose) |
| 29 | QueryPathFromHashPart | SQLite | No — pure forward |
| 31 | QueryValidPaths | SQLite | No — pure forward |
| 36 | BuildDerivation | None | Yes (scheduler) |
| 40 | QueryMissing | None | Yes (goal manager) |
| 41 | QueryDerivationOutputMap | None | No — pure forward |
| 103+ | All extensions | Varies | Per-op |

## Appendix C: Handler-to-executor mapping

| Op | Handler | Executor |
|---|---|---|
| 1 | IsValidPathHandler | IsValidPathExecutor |
| 6 | None (auto-forward) | QueryReferrersExecutor |
| 7 | AddToStoreHandler (streaming) | None |
| 9 | BuildPathsHandler (scheduler) | None |
| 10 | None | EnsurePathExecutor |
| 11 | AddTempRootHandler (no-op) | None |
| 12 | AddIndirectRootHandler (no-op) | None |
| 14 | FindRootsHandler | None |
| 19 | SetOptionsHandler (no-op) | None |
| 20 | CollectGarbageHandler (admin) | None |
| 23 | None | QueryAllValidPathsExecutor |
| 26 | QueryPathInfoHandler | QueryPathInfoExecutor |
| 29 | None | QueryPathFromHashPartExecutor |
| 31 | None | QueryValidPathsExecutor |
| 32 | None | None (pure forward) |
| 33 | None | None (pure forward) |
| 34 | OptimiseStoreHandler (admin) | None |
| 35 | VerifyStoreHandler (admin) | None |
| 36 | BuildDerivationHandler (scheduler) | None |
| 37 | None | None (pure forward) |
| 38 | NarFromPathHandler (streaming) | None |
| 39 | AddToStoreNarHandler (streaming) | None |
| 40 | QueryMissingHandler (goal manager) | None |
| 41 | None | QueryDerivationOutputMapExecutor |
| 42 | None | None (pure forward) |
| 43 | None | None (pure forward) |
| 44 | AddMultipleToStoreHandler (streaming) | None |
| 45 | AddBuildLogHandler (admin) | None |
| 46 | BuildPathsWithResultsHandler (scheduler) | None |
| 47 | AddPermRootHandler (no-op) | None |
| 101 | PynixdCollectGarbageHandler (admin) | None |
| 103 | None | QueryPathInfosExecutor |
| 104 | None | QueryClosureExecutor |
| 105 | None (auto-forward) | QueryClosureWithInfoExecutor |
| 106 | None (auto-forward) | QueryDerivationOutputMapBatchExecutor |
| 107 | SignPathInfoHandler (admin) | None |
| 108 | ProbeSystemsHandler (internal) | None |
| 109 | ProbeFeaturesHandler (internal) | None |

Counts:
- **Handlers:** ~20 (build ops, streaming ops, admin ops, no-op ops, internal)
- **Executors without handlers:** ~10 (SQLite fast-paths, query ops)
- **Neither (pure wire forward):** ~23 ops

## Store protocol

Executors don't live on Store — they live in separate files — but they
access Store through a narrow, stable protocol.  This keeps the Store
class small and lets different backends implement the same interface.

```
Store protocol (small, stable):
├── store.db: Database | None          ← SQLite fast-paths
├── store.tracker: PathTracker          ← in-memory cache
├── store.forward(req) → WireResponse   ← daemon round-trip
├── store.transfer_conn()               ← streaming ops
├── store.path_info_cache               ← per-store LRU
└── store.store_id                      ← identity for logging

Not on Store:
├── SQL queries (prepared centrally, not per-store)
├── Business logic (in executors/handlers)
├── Auth / routing (in handlers)
└── Scheduler interaction (in build handlers)
```

A substituter store implements the same protocol:

```
HttpBinaryCacheStore:
├── db: None (always)                   ← no local DB
├── tracker: PathTracker (shared)       ← same in-memory cache
├── forward(req) → WireResponse         ← calls HTTP API, not daemon
├── transfer_conn → raises NotSupported ← no streaming
└── path_info_cache: {}                 ← empty, HTTP has its own cache
```

Executors don't care which backend they talk to.  An executor does
`if store.db is None: return None` and the dispatch loop falls through
to `store.forward()`.  The substituter's `forward()` handles it — HTTP
API call instead of daemon RPC.  Same wire format, different transport.

## Store class hierarchy

Two competing approaches:

### Option A: Virtual methods on Store base class

Each operation gets a method on Store that returns `None` by default.
Subclasses override the ones they can optimize.

```python
class Store(ABC):
    @abstractmethod
    async def forward(self, req: WireRequest) -> WireResponse:
        """Send request to backend, return response."""
        ...

    # ── Optimizable operations (return None = no fast-path) ──

    async def is_valid_path(self, req: IsValidPathRequest) -> IsValidPathResponse | None:
        return None

    async def query_path_info(self, req: QueryPathInfoRequest) -> QueryPathInfoResponse | None:
        return None

    async def query_all_valid_paths(self, req: QueryAllValidPathsRequest) -> QueryAllValidPathsResponse | None:
        return None
    # ... ~15 more

    # ── Operations that are never fast-tracked (always forward) ──
    # No method needed — they go directly to forward()


class DaemonStore(Store):
    """Talks to a Nix daemon over Unix socket or SSH."""
    async def forward(self, req): ...  # wire protocol


class LocalDaemonStore(DaemonStore):
    """DaemonStore with SQLite cache for fast local queries."""
    db: Database

    async def is_valid_path(self, req):
        if row := await self.db.execute(IS_VALID_PATH, (str(req.path),)):
            return IsValidPathResponse(valid=True)
        return None  # fall through to forward()

    async def query_path_info(self, req): ...
    # overrides ~10 query ops


class HttpBinaryCacheStore(Store):
    """HTTP substituter — no daemon, no DB."""
    async def forward(self, req):
        # translates to HTTP API calls
        ...
    # No overrides — is_valid_path returns None → forward() handles it
```

**Pros:**
- Handler is trivial: `resp = await store.is_valid_path(req)`; if None, `forward()`.
- Subclasses pick which ops to override. No capability flags.
- Nix-compatible pattern (C++ `Store` has virtual `queryPathInfo()` etc.)

**Cons:**
- Store class grows ~15 methods (one per optimizable op).  15 is manageable,
  53 is not — but only ~15 need methods.

### Option B: Protocol classes for capabilities

Store stays small.  Capabilities are Protocol classes that executors
can type-check against.

```python
class HasDB(Protocol):
    db: Database

class HasTracker(Protocol):
    tracker: PathTracker

class DaemonStore(Store, HasDB, HasTracker):
    ...

class HttpBinaryCacheStore(Store):  # no HasDB
    ...
```

Then an executor types its store parameter as `HasDB`, not `Store`:

```python
class IsValidPathExecutor(Executor):
    async def execute(self, req: WireRequest, store: HasDB) -> WireResponse | None:
        row = await store.db.execute(...)
```

The dispatch loop checks `isinstance(store, HasDB)` before calling the
executor.  If the store doesn't have the capability, the executor
is simply never called.

**Pros:**
- Store class stays tiny (just `store_id`, `forward()`, `transfer_conn()`).
- Executors declare their exact dependency.  Type checker verifies.
- New capabilities can be added without touching Store.

**Cons:**
- Two-phase dispatch: handler → check capability → executor.
- Protocol classes feel like Java interfaces.  Adds abstraction layer.

### Recommendation

Go with **Option A**, done properly as a full class hierarchy.  Each layer
adds capability, each subclass overrides only what it can optimize.

```
Store (ABC)
├── DaemonStore
│   └── DBDaemonStore
├── SubstituterStore
│   └── HTTPStore
└── (future) NanopynixStore
```

#### Coarse API on base Store

Store declares virtual methods for every operation, returning `None` by default:

```python
class Store(ABC):
    store_id: StoreId
    db: Database | None
    tracker: PathTracker

    @abstractmethod
    async def forward(self, req: WireRequest) -> WireResponse: ...

    # ~20 virtual fast-path methods, all return None by default
    async def is_valid_path(self, req) -> IsValidPathResponse | None: return None
    async def query_path_info(self, req) -> QueryPathInfoResponse | None: return None
    async def query_all_valid_paths(self, req) -> QueryAllValidPathsResponse | None: return None
    # ...
```

Handler is trivial — call the method, fall through to forward:

```python
class IsValidPathHandler(Handler):
    async def handle(self, ctx):
        req = await IsValidPathRequest.from_reader(ctx.read_ctx)
        if resp := await ctx.store.is_valid_path(req):
            return resp
        return await ctx.store.forward(req)
```

#### Layer responsibilities

| Layer | Responsibility |
|---|---|
| `Store` (ABC) | Declares virtual methods, owns `store_id`/`db`/`tracker` |
| `DaemonStore` | Implements `forward()` via daemon protocol. All virtual methods return `None` → forwarded. |
| `DBDaemonStore(DaemonStore)` | Overrides ~10 query ops with SQLite. Falls back to DaemonStore for the rest. |
| `SubstituterStore` | Overrides `forward()` to raise `NotImplementedError` for most ops. Provides `download()` instead. |
| `HTTPStore(SubstituterStore)` | Implements substitutable ops via HTTP binary cache API. |

#### Avoiding huge files

If `Store` or `DBDaemonStore` grows too large, split the virtual methods
into mixin modules using a folder structure:

```
pynixd/store/
├── __init__.py
├── _base.py              # Store ABC + class skeleton
├── _methods/
│   ├── is_valid_path.py  # def is_valid_path(self, req): ...
│   ├── query_path_info.py
│   └── ...
├── daemon.py             # DaemonStore
├── db_daemon.py          # DBDaemonStore(DaemonStore)
├── substituter.py        # SubstituterStore
└── http.py               # HTTPStore
```

Each `_methods/*.py` defines a free function that takes `self` as first
arg.  `_base.py` imports and attaches them as methods.  No circular
imports because `_methods/` modules only import from `serde`, not from
`_base.py`.  Example:

```python
# _methods/is_valid_path.py
async def _is_valid_path(self, req):
    if self.db is None:
        return None
    async with self.db.execute(IS_VALID_PATH, (str(req.path),)) as cursor:
        row = await cursor.fetchone()
    if row:
        return IsValidPathResponse(valid=True)
    return None

# db_daemon.py
from ._methods.is_valid_path import _is_valid_path

class DBDaemonStore(DaemonStore):
    is_valid_path = _is_valid_path
    # ...
```

This keeps each file small and methods testable in isolation without
constructing a full Store.

1. **`Store.execute()` API rework** — currently `store.execute(req)` does
   double duty as "try local executor, else forward."  Should this be
   replaced by explicit `store.forward(req)` for pass-through and
   separate executor calls?

2. **Executor instance lifecycles** — stateless per call (re-created each
   time) or long-lived singletons?  Stateless is simpler.  Long-lived
   could hold prepared SQL statements.

3. **Streaming operations** — `NarFromPath`, `AddToStoreNar`, `AddToStore`,
   `AddMultipleToStore` all override `handle` to manage raw byte streaming.
   How do handlers for these work?  They probably need to bypass the
   normal serde flow entirely and talk directly to the transport.

4. **Auth placement** — currently `Role.ADMIN` checks live in `handle()`.
   Should they move to the dispatch loop (check role before calling
   handler), or stay in handlers?  The loop approach is DRY but less
   flexible (some ops have partial admin gating).


## Answers from user

### Unnumbered
- IsValidPath doesn't need auth?

### Numbered
1. I'm not entirely sure how to deal with this, this is a very design-heavy question. Since we're working on making parts of pynixd reusable we need to support not having an SQLite database to query. It's not unreasonable that we split the localsocketstore into two types, one with database any one without?
2. Stateless per call, but if we store the queries inside the executor we could use the subclass init "hook" to register the queries somewhere central that manages prepared statements.
3. Since we skipped the "NAR fields" in these types the handle method can call on their from_readers but they need to manage transport themselves as well yeah, that's why the handle method has access to DaemonProxy and such.
4. They live well in handle for now, the authentication system is VERY bare-bones currently and we don't want to mess with it now.

## Exploring alternatives

### Q1: `Store.execute()` rework

**A: Split LocalSocketStore into DB/non-DB**
| | With DB | Without DB |
|---|---|---|
| `can_handle` | True for all | Always False |
| Fallback | Graceful (non-DB clients can still forward) | Pure proxy |
| Overhead | None — the DB check is just `self.db is not None` | None |

Alternative — **a single Store with optional DB, and executors check `store.db` at call time**:
| Approach | Pros | Cons |
|---|---|---|
| Split classes | Clear capability model, no runtime checks | Two types to maintain, config decides which |
| Single class + optional DB | Simpler codebase, fewer classes | Executors do `if store.db is None` guard every call |

The split is cleaner if the non-DB path is genuinely different (e.g., a read-only proxy that never caches). If it's just the same store without optimizations, a single class with `can_handle` returning `store.db is not None` is simpler.

**B: Store gets `forward(req)` and executors live outside Store**
| Step | Responsibility |
|---|---|
| `store.forward(req)` | Serialize, send to daemon, return raw bytes |
| `executor.execute(req, store)` | Try fast-path, return response or None |
| Dispatch loop | Orchestrates: deserialize → executor → forward → write |

Alternative — **executor is a method on Store with a decorator for registration**:
```python
class Store:
    @executor_for(1)  # IsValidPath
    async def is_valid_path(self, req: WireRequest) -> WireResponse | None:
        if self.db is None:
            return None
        ...
```
Centralizes executors but ties them to Store's implementation.  The
separate Executor class is more reusable (e.g., a SQLite-only store
bundle).

### Q2: Executor lifecycles

**A: `__init_subclass__` registers queries centrally**

Alternative approaches:

1. **Query as class attribute** — executor declares `QUERY = "SELECT ..."`, prepared by a global `QueryManager` at registration time.  Executor instances are stateless shells that call `query_manager.execute(self.QUERY, params)`.

2. **Executor receives a `StoreSession`** — instead of `store.db`, the executor gets a short-lived session object with prepared statements for this call.  The session owns connection lifecycle, the executor just calls methods.

3. **Hybrid: executor method returns a coroutine** — `prepare()` called once at registration (compiles SQL, binds parameters), returns a factory.  At call time the factory receives a connection and returns a result.

| Approach | Concurrency | Complexity |
|---|---|---|
| Class attr + QueryManager | Safe (manager handles pooling) | Medium |
| StoreSession | Safe per call | Low |
| Factory pattern | Safe, zero runtime setup | High (two-phase call) |

### Q3: Streaming operations

**A: Handler calls `from_reader` then manages transport via DaemonProxy**

Alternative approaches:

1. **Two handler methods** — `handle_before_stream(req, ctx)` returns a setup object, then the transport loop pumps raw bytes through `handle_stream(ctx, setup)`.  Splits the structured header from the streaming body.

2. **Streaming ops are NOT handlers** — they stay as `OpRequest.handle()` overrides that talk directly to `DaemonProxy`.  The dispatch loop special-cases them (ops 7, 38, 39, 44 — all streaming).  Simpler, but keeps legacy pattern.

3. **Handler returns a generator** — `handle()` yields chunks.  The transport loop writes each chunk to the client as it arrives.  For streaming requests, `handle()` is itself an async generator.

| Approach | Code reuse | Implementation effort |
|---|---|---|
| Two-method handler | High (structured header uses serde) | Medium |
| Skip handler, keep override | None | Low (but keeps legacy) |
| Handler as generator | High (unified interface) | High (generator bookkeeping) |

For the initial implementation, option 2 (keep streaming as overrides)
is pragmatic.  Option 1 can be added later when the non-streaming
handler pattern is proven.

### Q4: Auth placement

**A: Auth stays in `handle()`**

Alternative — **declarative auth on the handler class**:

```python
class OptimiseStoreHandler(Handler):
    op: ClassVar[int] = 34
    min_role: ClassVar[Role] = Role.ADMIN
    
    async def handle(self, ctx): ...
```

The dispatch loop checks `min_role` before calling `handle()`.  Admin
rejection is uniform: send stderr error, return None.

Keeps auth DRY without hardcoding it in the loop — each handler
declares its own requirement.  Non-admin handlers (or handlers with
partial gating) don't set `min_role`.

But the user's answer is clear: auth stays in handle for now.  The
declarative approach can be added later when the auth system matures.
