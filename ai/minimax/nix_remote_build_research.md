# Nix Remote Build Research

## Overview

This document describes how Nix's `--builders` remote building feature works, researched by examining the Nix (Lix) source code and comparing with Rio-build's implementation.

## Key Insight: Two Different Remote Build Mechanisms

Nix has TWO different mechanisms for remote builds:

### 1. Build Hook Mechanism (Standard --builders)
- Uses Cap'n Proto RPC via `hook-instance.cc`
- Client communicates with build hook which coordinates builds
- The hook can be an external process or internal mechanism

### 2. Daemon Protocol Direct (ssh-ng://, unix://)
- Client connects directly to daemon via worker protocol
- Uses operations like BuildDerivation (40), QueryPathInfo (26), NarFromPath (46)
- This is what pynixd implements

## Client-Side Behavior: How nix build works

When running `nix build --builders unix://...`:

1. **Nix decides to use remote builder**:
   - In `derivation-goal.cc:tryBuildHook()` - checks if build should be remote
   - Prefers local builds unless `--max-jobs 0` or `preferLocalBuild = false`

2. **Nix spawns a daemon process**:
   - Creates a child process running `nix daemon`
   - This daemon connects to the specified store (unix socket in our case)
   - The daemon performs all protocol operations

3. **Operations flow**:
   - Op 7 (IsValidPath) - check if path is valid
   - Op 40 (BuildDerivation) - submit the build
   - Op 26 (QueryPathInfo) - check build outputs
   - Op 46 (NarFromPath) - retrieve output NARs (if needed)

4. **How client gets results**:
   - The spawned daemon queries the remote store (pynixd)
   - Gets path info and NARs from pynixd
   - The CLIENT's machine gets the NARs imported into its local store

## The Key Question: How does the CLIENT get built paths?

After a successful remote build, how does nix get the built paths from pynixd into the client's local store?

### Method 1: NarFromPath + AddToStore (Most Likely)
1. Client's daemon queries NarFromPath from pynixd
2. Client's daemon calls AddToStore/AddToStoreNar to import to local store

### Method 2: Store path injection (NOT used with --builders)
- Not applicable - the user explicitly rejected this approach

### Method 3: Serve Protocol AddToStoreNar
- Used for SSH builds with serve protocol
- Not used for unix socket builds

## Protocol Operations Observed

From user's test log:
```
performing daemon worker op: 7    # IsValidPath
performing daemon worker op: 40   # BuildDerivation
performing daemon worker op: 26   # QueryPathInfo
```

Note: Op 46 (NarFromPath) was NOT seen in the log, which suggests the client may have failed before attempting to fetch the NARs.

## Relevant Files in Nix Source

| File | Purpose |
|------|---------|
| `libstore/daemon.cc` | Main daemon implementation - handles client connections |
| `libstore/worker-protocol.hh` | Protocol operation numbers (Op::BuildDerivation = 36, etc.) |
| `libstore/serve-protocol.hh` | Serve protocol for SSH builds (AddToStoreNar = 9) |
| `libstore/build/derivation-goal.cc` | Build dispatch logic - decides local vs remote |
| `libstore/build/hook-instance.cc` | Build hook RPC implementation |
| `libstore/remote-store.cc` | Remote store client for connecting to daemons |
| `libstore/export-import.cc` | Import/export paths (nar import) |

## Protocol Version

- Worker Protocol version: 1.35
- Magic numbers: WORKER_MAGIC_1 = 0x6e697863, WORKER_MAGIC_2 = 0x6478696f
- STDERR_NEXT = 0x6f6c6d67, STDERR_LAST = 0x616c7473

## Next Investigation

Need to find where exactly the client's spawned daemon imports the built paths into the client's local store. This likely happens via:
1. NarFromPath to get NAR data from pynixd
2. AddToStore/AddToStoreNar to import to local

The "builder for ...drv failed" error might be happening because:
1. Build succeeded but outputs not recognized as valid
2. Store path mismatch between pynixd and client's daemon
3. Something in the build result validation is failing