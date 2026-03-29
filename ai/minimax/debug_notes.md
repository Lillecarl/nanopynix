# pynixd BuildDerivation Debugging Notes

## Architecture (Critical Understanding)

### Three Stores:
1. **Client store**: The machine's local store. pynixd doesn't know about this. Client gets build results back here.
2. **pynixd local store**: The "source of truth" store for pynixd. All NARs land here. pynixd responds to queries from here. This is set via `nix daemon --store $path --stdio`.
3. **Builder/Backend stores**: Used to perform actual builds. pynixd sends paths from its local store to backends before BuildDerivation.

### Expected Flow with --builders:
1. Client connects to pynixd
2. pynixd has its local store (where NARs land)
3. Build happens on backends, outputs pulled to pynixd's local store
4. pynixd must send outputs to CLIENT's store (not keep in pynixd's store!)
5. Client receives built paths in its own store

## Current Issue
BuildDerivation succeeds, outputs are in pynixd's local store, QueryPathInfo succeeds when queried through pynixd, but client still fails.

Flow from logs:
1. BuildDerivation response: status=0, outPath=/nix/store/xxx-test-...
2. QueryPathInfo: local hit - path found in pynixd's store
3. ERROR: "builder for ...drv failed on remote builder" - Nix tries to BUILD AGAIN!

The client's spawned daemon doesn't have the output, so it tries to rebuild.

## Solution Needed
After BuildDerivation succeeds, pynixd must push the outputs to the CLIENT's store via the client's connection.

## Files Modified
- pynixd/operations/base.py - BuildResult JSON handling + debug logging
- pynixd/router.py - QueryPathInfo path fix
- pynixd/proxy.py - BuildDerivation logging
- pynixd/scheduler.py - Added debug logging
- pynixd/unix_server.py - Shared local store
- pynixd/subprocess_store.py - Store path handling
- pynixd/store.py - Store path property
- tests/conftest.py - Shared local store fixture
- default.nix - Added -p no:cacheprovider to pytest
