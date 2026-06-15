# Store Class Hierarchy — Implementation Plan

## Phase 1: Virtual methods on Store base class

Add ~15 virtual methods to `pynixd/store/base.py`.  Each returns `None`
by default.  `Store.execute()` already checks executors first; now the
executor IS the virtual method call.

```python
class Store(ABC):
    db: Database | None          # removed in Phase 3 — moves to LocalDBStore
    tracker: PathTracker         # removed in Phase 3 — moves to LocalDBStore

    @abstractmethod
    async def forward(self, request: OpRequest) -> OpResponse:
        """Send request to backend daemon, return response."""
        ...

    # ── Fast-path hooks (return None → fall through to forward) ──

    async def is_valid_path(self, request) -> OpResponse | None: return None
    async def query_path_info(self, request) -> OpResponse | None: return None
    async def query_all_valid_paths(self, request) -> OpResponse | None: return None
    async def query_valid_paths(self, request) -> OpResponse | None: return None
    async def query_path_from_hash_part(self, request) -> OpResponse | None: return None
    async def query_closure(self, request) -> OpResponse | None: return None
    async def query_closure_with_info(self, request) -> OpResponse | None: return None
    async def query_path_infos(self, request) -> OpResponse | None: return None
    async def query_derivation_output_map_batch(self, request) -> OpResponse | None: return None
    async def query_missing(self, request) -> OpResponse | None: return None
```

**Test checkpoint:** Run full functional suite.  Behavior unchanged — all
methods return None, executors still fire via `EXECUTOR_REGISTRY`.

## Phase 2: Move executors into virtual method overrides

This is where executors become Store method overrides.  Instead of
`EXECUTOR_REGISTRY` dispatch in `Store.execute()`, the executor logic
lives directly in the overridden method.  `Store.execute()` just calls
the virtual method.

**Order:** One method at a time, same pattern as handler migration.
Start with `is_valid_path`.

```python
# Before (executor in separate file):
class Store:
    async def execute(self, request, ...):
        if executor_cls := _get_executor(op):
            if result := await executor.execute(request, self):
                return result
        return await request.execute(...)

# After (executor logic in virtual method on LocalDBStore):
class LocalDBStore(LocalStore):
    async def is_valid_path(self, request):
        if self.tracker.has_path(request.path):
            return IsValidPathResponse(valid=True)
        if row := await self.db.execute(IS_VALID_PATH, ...):
            return IsValidPathResponse(valid=True)
        return None  # falls through to DaemonStore.forward()
```

**What happens to executors?**  The executor files become the method
body source.  After all executors are moved, `EXECUTOR_REGISTRY` and
`pynixd/executors/` can be deleted.

**Test checkpoint after each method:** Run relevant functional tests.

## Phase 3: Create DaemonStore

Extract `forward()` from the current `Store.call()` method.  This is a
pure wire protocol concern — serialize request to daemon, read response.

`Store` no longer has `db` or `tracker`.  Those move to `LocalDBStore`.

```python
class DaemonStore(Store):
    """Talks to a Nix daemon over the wire protocol."""
    
    async def forward(self, request):
        await self.connection.write(request)
        return await self.connection.read_response()
```

**Test checkpoint:** At this point, `DaemonStore` has no overrides —
every operation falls through to `forward()`.  Functional tests should
still pass because the old `Store.execute()` path still works alongside.

## Phase 4: Create LocalStore and LocalDBStore

`LocalStore(DaemonStore)` — Unix socket transport, no DB.
`LocalDBStore(LocalStore)` — adds `db` + `tracker`, overrides query ops.

```python
class LocalStore(DaemonStore):
    """Unix socket connection to local daemon. No DB access."""
    
    async def connect(self):
        return await asyncio.open_unix_connection(self.socket_path)


class LocalDBStore(LocalStore):
    """LocalStore with SQLite database for fast-path queries."""
    
    def __init__(self, db_path: Path, ...):
        self.db = Database(db_path)
        self.tracker = PathTracker()
    
    async def is_valid_path(self, request):
        if self.tracker.has_path(request.path):
            return IsValidPathResponse(valid=True)
        if row := await self.db.execute(...):
            return IsValidPathResponse(valid=True)
        return await super().is_valid_path(request)  # → DaemonStore.forward()
```

**Test checkpoint:** Create `LocalDBStore` in test fixtures, run full
suite.  The old `Store` base becomes an abstract interface.

## Phase 5: SSH stores

```python
class SSHSubprocessStore(DaemonStore):
    """SSH connection via asyncssh subprocess."""
    async def connect(self):
        return await asyncssh.connect_subprocess(...)


class SSHSocketStore(DaemonStore):
    """SSH connection via asyncssh socket."""
    async def connect(self):
        return await asyncssh.connect_socket(...)
```

These are thin — they only override `connect()`.  Everything else
delegates to `DaemonStore.forward()`.

## Phase 6: Substituter stores

Substituters don't talk to daemons at all — they have a different wire
protocol.  They get their own base class.

```python
class SubstituterStore(Store):
    async def forward(self, request):
        raise NotImplementedError("Substituters don't forward to daemons")
    
    @abstractmethod
    async def download(self, path) -> bytes: ...


class HttpBinaryCacheStore(SubstituterStore):
    async def download(self, path):
        # HTTP GET /nix-cache-info, then /nar/<hash>.nar
        ...
```

## Migration order summary

| Phase | What | Risk | Test after? |
|---|---|---|---|
| 1 | Virtual methods on Store | Low — all return None | Full suite |
| 2 | Move executors into overrides | Medium — one method at a time | Per-method tests |
| 3 | DaemonStore with forward() | Medium — extract wire code | Full suite |
| 4 | LocalStore + LocalDBStore | Medium — new classes | Full suite |
| 5 | SSH stores | Low — thin wrappers | Connection tests |
| 6 | Substituter stores | Low — separate hierarchy | Substituter tests |

## Files to create

```
pynixd/store/
├── _base.py            # Store ABC + virtual methods (Phase 1–2)
├── daemon.py           # DaemonStore (Phase 3)
├── local.py            # LocalStore (Phase 4)
├── local_db.py         # LocalDBStore (Phase 4)
├── ssh_subprocess.py   # SSHSubprocessStore (Phase 5)
├── ssh_socket.py       # SSHSocketStore (Phase 5)
├── substituter.py      # SubstituterStore (Phase 6)
├── http.py             # HttpBinaryCacheStore (Phase 6)
└── __init__.py
```
