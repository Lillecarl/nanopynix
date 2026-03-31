# pynixd Development Mandates

This document defines the foundational architectural patterns and engineering standards for `pynixd`. These instructions take precedence over general defaults.

## 1. Version Control: Jujutsu (jj)
- **Tool**: Use `jj` (Jujutsu), NOT `git`.
- **Committing**: Prefer `jj commit -m "..."` to finish a task. It creates a new revision and provides a clean working copy.
- **Paging**: Always include `--no-pager` in all `jj` commands to ensure non-interactive execution.

## 2. Core Architectural Pattern: Request-Driven Execution
`pynixd` follows a strict three-tier execution pattern to separate protocol IO from business logic.

1. **Server Dispatch** (`OpRequest.handle(proxy)`): 
   - Entry point for the `DaemonProxy`.
   - Decodes the request from the client wire.
   - Delegates logic to the store: `return await proxy.local_store.execute(request)`.
   - *Streaming operations* (like `NarFromPath` or `AddToStore`) override this to handle raw byte piping.

2. **Logic Hook** (`OpRequest.execute(store, client=None, suppress_last=False)`):
   - Where the "recipe" for an operation lives.
   - Implements optimizations (SQLite fast-paths, memory caches).
   - If no optimization exists, falls back to the wire: `return await store.call(self, client=client, suppress_last=suppress_last)`.

3. **Store Executor** (`Store.execute(request, ...)`):
   - Simple polymorphic dispatcher that calls `request.execute(self, ...)`.

4. **Transport** (`Store.call(request, ...)`):
   - Low-level wire protocol implementation.
   - Handles connection pooling, protocol magic, and handshake.

## 3. Stderr & Logging
- **`StderrBuffer`**: All buffered responses MUST include a `StderrBuffer` in their `stderr` field.
- **Real-time Forwarding**: If a `ClientConn` is provided to `execute()`, logs MUST be forwarded to `client.queue` in real-time while also being buffered in the response.
- **`suppress_last`**: When executing sub-operations (e.g., builds within a `BuildPaths` request), intermediate `STDERR_LAST` messages MUST be suppressed to avoid confusing the client.
- **Transparency**: No-op or cached operations MUST inject a `StderrNext` message (e.g., `"pynixd: IsValidPath (SQLite hit)"`) into the buffer for transparency.

## 4. Engineering Standards
- **Validation**: ALWAYS run `just check` before committing. This runs `ruff` (formatting/linting) and `pyright` (type checking).
- **Type Safety**:
  - Use `from __future__ import annotations`.
  - NEVER use string type hints (e.g., `"Store"`). 
  - Use `if TYPE_CHECKING:` blocks for cross-module imports.
- **No-ops**: Restricted operations (like `SetOptions`, `AddPermRoot`, `AddIndirectRoot`) must be implemented as no-ops that return success (`0` or `EmptyResponse`) and log their status to stderr.
- **HTTP Cache Streaming**: If a NAR transfer fails after the `200 OK` header is sent, the server MUST abruptly close the connection to signal failure to the client. Full buffering to avoid this is not supported due to memory constraints.

## 5. Build Logic
Builds are the only "complex" operations in `pynixd`. They are handled via a global `BuildQueue` and a DAG-aware `Scheduler`. 
- `BuildPaths` and `BuildPathsWithResults` are decomposed into individual `BuildDerivation` requests.
- Each build executes in a spawned task, surviving client disconnects.
- Outputs are automatically pulled into the `LocalStore` upon successful completion.
