# pynixd Development Mandates

This document defines the foundational architectural patterns and engineering standards for `pynixd`. These instructions take precedence over general defaults.

## 1. Version Control: Jujutsu (jj)
- **Tool**: Use `jj` (Jujutsu), NOT `git`.
- **Committing**: Prefer `jj commit -m "..."` to finish a task. It creates a new revision and provides a clean working copy.
- **Squashing**: If your changes are a fixup for the last commit, prefer `jj squash --use-destination-message` to keep the commit message or `jj squash -m "..."` to update the commit message
- **Paging**: Always include `--no-pager` in all `jj` commands to ensure non-interactive execution.

## 2. Core Architectural Pattern: Request-Driven Execution
`pynixd` follows a strict three-tier execution pattern to separate protocol IO from business logic.

Important Nix protocol version support matrix:
Builder stores: >= 1.32 (nixbuild.net is 1.32)
Local stores: >= 1.35 (Lix is 1.35)
Pynixd will adversise 1.38 support even if local_store is 1.35 and translate where appropriate

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
- **Validation**: ALWAYS run `just precommit` before committing. This runs `ruff` (formatting/linting), `pyright` (type checking) and functionality tests.
- **Type Safety**:
  - NEVER use string type hints (e.g., `"Store"`). Use `from __future__ import annotations` where needed for `TYPE_CHECKING` imports and forward references.
  - Use `if TYPE_CHECKING:` blocks for cross-module imports.
  - **Imports**: All imports should be at the top of the file. Lazy imports inside functions are only acceptable to break circular import cycles.
- **No-ops**: Restricted operations (like `SetOptions`, `AddPermRoot`, `AddIndirectRoot`) must be implemented as no-ops that return success (`0` or `EmptyResponse`) and log their status to stderr.
- **HTTP Cache Streaming**: If a NAR transfer fails after the `200 OK` header is sent, the server MUST abruptly close the connection to signal failure to the client. Full buffering to avoid this is not supported due to memory constraints.
- **pathlib.Path**: Use pathlib.Path when dealing with any strings that aren't Nix daemon protocol related. Convert to string as late as possible if needed

## 5. Build Logic
Builds are the only "complex" operations in `pynixd`. They are handled via a global `BuildQueue` and a DAG-aware `Scheduler`. 
- `BuildPaths` and `BuildPathsWithResults` are decomposed into individual `BuildDerivation` requests.
- Each build executes in a spawned task, surviving client disconnects.
- Outputs are automatically pulled into the `LocalStore` upon successful completion.

## 6. Execution Sanity & Recovery
- **Halt on Ambiguity**: If a tool output indicates potential corruption (e.g., duplicate declarations in a `replace` output, unexpected truncations), or if you lose track of the file state relative to the VCS, **STOP immediately**. Do not attempt blind recovery (like `write_file` with partial content).
- **Verify Before Rewrite**: Before using `write_file` to "fix" a large file, you MUST have read the *entire* file in the current turn to ensure no data loss.
- **VCS Truth**: If `jj status` or `jj diff` contradicts your internal model of the changes, re-sync by reading the files from disk before taking further action. Do not guess.

## 7. Test Suite Rules

### Directory Structure
- **`tests/functional/`** — active end-to-end and integration tests
- **`tests/benchmark/`** — performance benchmarks
- **`tests/legacy/`** — old tests, do not modify

### Test Store Conventions
- All test stores MUST use the `/tmp/pynixd-store-` prefix (defined as `STORE_PREFIX` in `tests/conftest.py`).
- Use `rmtree_robust_glob(f"{STORE_PREFIX}*")` in fixtures to clean up leftover stores.
- Only a few select tests should run against the root store (`store_path=Path("/")`). Most tests should use isolated stores with the prefix.

### Test Helpers
- **`run_captured(cmd, **kwargs)`** — runs a subprocess, returns `(rc, stdout, stderr)`.
- **`run_logged(cmd, **kwargs)`** — runs a subprocess, streams output through structlog in real-time.
- Both helpers auto-set `NIX_SSHOPTS` if not already present.
- Use `env.str("NIX_BIN", "nix")` and `env.str("LIX_BIN", "nix")` from the `environs` singleton for binary paths.

### Test Design
- Keep tests simple and explicit. Avoid over-engineered abstractions.
- Construct commands as plain lists so the exact invocation is visible at a glance.
- Use `pytest.fixture(autouse=True)` for per-test cleanup (store directories, etc.).

### Running Validation Commands
- **NEVER pipe away output** from `just check`, `just precommit`, or `pytest` — the full output contains failure details you need to diagnose issues.
- If output is too large for context (failing tests produce heaps of logs), redirect to a file: `pytest ... > /tmp/test-output.txt 2>&1`, then read specific sections.
- Do NOT use `tee` when redirecting — it doubles context consumption.
- If you must limit output, use `tail -N` on the file afterwards, never pipe the command itself.
- You do NOT need to specify pytest timeout, the configured 120s is enough per test.

## 8. Async Task & Lifecycle Rules
- **Structured Concurrency**: For short-lived, bounded concurrent operations (e.g., fanning out requests, concurrent streams within a single handler, or parallel passes like GC), ALWAYS prefer `asyncio.TaskGroup` over `asyncio.gather` or manual `create_task` management.
- **Task Tracking**: Long-lived daemon components (Servers, Pools, Monitors) should continue using explicit `start()`/`close()` lifecycle methods. All background tasks created via `asyncio.create_task` in these components MUST be tracked (e.g., in a list or as a class attribute) and properly cleaned up during the component's `close()` or `stop()` method.
- **Graceful Shutdown**: When awaiting a cancelled background task during shutdown, ALWAYS use `with contextlib.suppress(Exception, asyncio.CancelledError):`. This ensures that if a task failed with an unhandled exception during its lifetime, that exception does not crash the shutdown sequence.
- **Orphaned Tasks**: Avoid orphaned tasks. If a `TaskGroup` cannot be used, ensure helper tasks use a `try...finally` block to guarantee they are cancelled and awaited if the primary operation fails.

#### Good examples
- just test
- just precommit
- pytest tests/functional/test_ca_ops.py &> /tmp/test_output.txt
- pytest tests/functional/test_ca_ops.py::test_ca_simple_build_root_store &> /tmp/test_output.txt
