# pynixd Build Flow Analysis

## Test Setup
```python
# From tests/conftest.py - nix_build function
cmd = [
    "nix", "build",
    "--builders", builders,  # unix://socket_path
    "--max-jobs", "0",        # ALL builds go to remote
    ...
]
```

The test uses `--max-jobs 0` which forces ALL builds to go to pynixd via --builders.

## Expected Flow

1. **Client (nix CLI)** decides to build
2. **Client spawns daemon** connecting to pynixd via unix socket
3. **Daemon operations** (via worker protocol):
   - Op 7: IsValidPath - check if drv is valid
   - Op 40: BuildDerivation - submit build to pynixd
   - Op 26: QueryPathInfo - check if outputs exist
   - Op 46: NarFromPath - retrieve output NARs

4. **pynixd handles BuildDerivation**:
   - Enqueues to build queue
   - Scheduler picks up build
   - Executes on remote builder
   - Pulls outputs to pynixd's local store
   - Returns BuildResult to client

5. **Client imports outputs**:
   - Calls NarFromPath to get NAR data from pynixd
   - Imports into client's local store via AddToStoreNar

## What We're Seeing

- BuildDerivation: Returns status=0 (success)
- QueryPathInfo: Returns valid path (pynixd has it)
- But client fails with: "builder for ...drv failed on remote builder"

This means after BuildDerivation returns success, something else fails.

## Key Questions

1. **Does the client call NarFromPath?**
   - User's log shows op 46 was called
   - Need to check if pynixd properly serves NARs for built outputs

2. **Does built_outputs in BuildResult contain correct paths?**
   - The built_outputs dict maps drvOutput -> Realisation
   - Realisation contains outPath, narHash, etc.
   - Need to verify format matches what nix expects

3. **Is there a store path mismatch?**
   - pynixd's local store path might differ from client's expected path
   - Need to verify what store path pynixd's daemon reports

## Investigation Needed

1. Add debug logging to trace:
   - What built_outputs are returned in BuildResult
   - What paths NarFromPath is called with
   - What data is returned by NarFromPath

2. Check nix source for validation:
   - What does nix check after receiving BuildResult?
   - What causes "builder failed" error after successful build?

3. Compare with working remote builder:
   - How does ssh-ng:// or other remote builders handle this?
   - What differences in protocol handling?

## Files to Debug

- pynixd/scheduler.py - Build execution and output pulling
- pynixd/router.py - Protocol handling
- pynixd/local_store.py - NAR serving

## Hypothesis

The issue might be in how built_outputs are serialized in BuildResult. Need to verify:
1. outPath is correct (full /nix/store/... path)
2. Realisation JSON format is correct
3. NAR data is properly available via NarFromPath