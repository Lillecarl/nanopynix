# Rio-build Nix Daemon Protocol Implementation

## Overview

Rio-build implements a complete Nix daemon protocol proxy with:
- **rio-gateway**: Handles Nix client connections via worker protocol
- **rio-scheduler**: Schedules builds to workers
- **rio-worker**: Executes builds via nix-daemon in sandbox
- **rio-store**: Provides NAR storage

This is highly relevant to pynixd as it solves similar problems.

## Key Files

### Protocol Implementation (rio-nix crate)

| File | Purpose |
|------|---------|
| `rio-nix/src/protocol/opcodes.rs` | Defines WorkerOp enum (BuildDerivation=36, AddToStoreNar=39, etc.) |
| `rio-nix/src/protocol/wire/mod.rs` | Wire primitives: read_u64, read_string, framed streams |
| `rio-nix/src/protocol/build.rs` | BuildResult, BuildStatus, read/write functions |
| `rio-nix/src/protocol/stderr.rs` | STDERR loop constants and writer |
| `rio-nix/src/protocol/client.rs` | Client-side protocol handling |
| `rio-nix/src/protocol/handshake.rs` | Server handshake (MAGIC numbers) |

### Gateway Handler (rio-gateway crate)

| File | Purpose |
|------|---------|
| `rio-gateway/src/handler/mod.rs` | Opcode dispatch, SessionContext |
| `rio-gateway/src/handler/build.rs` | handle_build_derivation, handle_build_paths |
| `rio-gateway/src/handler/opcodes_write.rs` | Store operations |

### Worker (rio-worker crate)

| File | Purpose |
|------|---------|
| `rio-worker/src/executor/daemon/mod.rs` | Spawn nix-daemon, run builds |
| `rio-worker/src/executor/daemon/stderr_loop.rs` | Read daemon output, stream logs |

## How Builds Work in Rio

### 1. Build Request Flow (Gateway)

```
Client -> wopBuildDerivation (opcode 36) -> Gateway
   - Reads drv_path string
   - Reads BasicDerivation (outputs, inputSrcs, platform, builder, args, env)
   - Reads build_mode (u64)
```

Key code:
```rust
// From build.rs
let drv_path_str = wire::read_string(reader).await?;
let basic_drv = read_basic_derivation(reader).await?;
let build_mode = BuildMode::try_from(wire::read_u64(reader).await?)?;
```

### 2. DAG Reconstruction

Rio cannot build from BasicDerivation alone. It:
1. Resolves full Derivation from drv_cache (uploaded via wopAddToStoreNar)
2. Reconstructs DAG using `translate::reconstruct_dag()`
3. Validates DAG
4. Submits to scheduler via gRPC

### 3. Build Execution (Worker)

Worker spawns `nix-daemon --stdio`:
```rust
// Spawn daemon
client_handshake(stdout_ref, &mut stdin).await?;
client_set_options(stdout_ref, &mut stdin).await?;
wire::write_u64(&mut stdin, WorkerOp::BuildDerivation as u64).await?;
// ... send derivation
```

## How Results Are Returned to Clients

### STDERR Loop Pattern

Every Nix protocol response uses STDERR streaming:
1. STDERR messages during operation
2. STDERR_LAST signals end
3. Result data written after STDERR_LAST

### Build Result Format

```
- status: u64 (0=Built, 1=Substituted, etc.)
- error_msg: string
- times_built: u64
- is_non_deterministic: bool
- start_time: u64
- stop_time: u64
- built_outputs: count + (drv_output_id, realisation JSON)*
```

### Flow: Worker -> Gateway -> Client

1. **Worker**: Reads daemon's STDERR loop, streams logs, reads BuildResult
2. **Gateway**: Receives BuildEvent stream from scheduler, translates to STDERR messages, sends STDERR_LAST + BuildResult
3. **Client**: Reads STDERR messages, then reads BuildResult

## Key Implementation Details

### 1. Cancel-Safe STDERR Reading

```rust
// Spawn owned reader task - not cancelled on timeout
let reader_task = tokio::spawn(async move {
    loop {
        let msg = read_stderr_message(&mut reader).await;
        if msg_tx.send(msg).await.is_err() { break; }
    }
    reader
});
```

### 2. Log Batching

- 100ms timeout, 64-line limit
- Prevents overwhelming scheduler

### 3. Build Output Resolution

For BuildPathsWithResults, Rio enriches BuildResult:
```rust
results.push(build_result.with_outputs_from_drv(drv_obj, &hash_hex));
```

## Wire Format Summary

- Integers: little-endian u64
- Strings: u64(length) + data + padding to 8-byte boundary
- Collections: u64(count) + elements
- Framed streams: u64(chunk_len) + chunk_data (NO padding), terminated by u64(0)

## Relevant Opcodes

```rust
// From opcodes.rs
IsValidPath = 7,
QueryPathInfo = 26,
BuildDerivation = 36,
BuildPaths = 9,
BuildPathsWithResults = 46,
AddToStore = 38,
AddToStoreNar = 39,
AddMultipleToStore = 40,
NarFromPath = 46,
```

## Key Insight for pynixd

Rio-build handles the full build cycle including:
1. Accepting BuildDerivation from clients
2. Delegating to workers
3. Returning BuildResult with proper STDERR streaming

This is exactly what pynixd needs to do. The key difference is:
- Rio has its own worker execution (spawns nix-daemon in sandbox)
- pynixd currently forwards to external builders

The critical part is returning proper BuildResult with built_outputs so the client can query and retrieve the NARs.