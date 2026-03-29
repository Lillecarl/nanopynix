# pynixd Architecture

**Version control: jujutsu (jj), NOT git**

## Stores

1. **Client store**: The machine's local store. Client connects to pynixd. pynixd can ONLY reply to requests - it cannot initiate anything to the client.

2. **pynixd local store**: pynixd's local store that it routes queries to. Where pynixd collects all store paths - both sent from clients and fetched from builders. This is the "source of truth" for pynixd.

3. **Builder stores**: Stores that pynixd connects to for actual builds. pynixd sends required input paths to builders and collects results/NARs from them.

## Protocol Flow

When using `--builders unix://...`:

1. Client's nix spawns a daemon connecting to the unix socket (pynixd)
2. Client sends daemon protocol operations to pynixd:
   - SetOptions (with builders config)
   - QueryValidPaths / QueryPathInfo (check what's needed)
   - AddMultipleToStore (upload drv files)
   - BuildDerivation (request build)
3. pynixd executes build on backend, pulls outputs to its local store
4. pynixd returns BuildResult with output paths to client

## Store Type Differences (Research from lix source)

Nix does NOT treat unix socket stores differently from other remote stores in terms of validity checking. Both UDSRemoteStore and RemoteStore use the same mechanism:

- `RemoteStore::isValidPathUncached` sends `WorkerProto::Op::IsValidPath` to daemon (remote-store.cc line 222)
- `UDSRemoteStore` inherits this behavior - it queries the daemon via socket protocol
- No special "local optimization" for unix stores

**Critical findings about UDSRemoteStore and NarFromPath are documented in `ai/minimax/`** - see `debug_notes.md` and `nix_remote_build_research.md` for details on why Unix socket stores don't work with remote builds.

## SetOptions We Send

All the standard options are sent correctly:
- keepFailed (Bool)
- keepGoing (Bool)
- tryFallback (Bool)
- verbosity (Verbosity)
- maxBuildJobs (Int)
- maxSilentTime (Time)
- verboseBuild (Verbosity)
- buildCores (Int)
- useSubstitutes (Bool)

The `builders` override tells nix WHERE to build, but doesn't tell the spawned daemon to use that store for post-build queries.

## Key Issue

After BuildDerivation returns status=0, the client's daemon should:
1. Call `worker.store.isValidPath(outputPath)` to verify outputs exist
2. Call `NarFromPath` to fetch the NAR data
3. Import to its local store

But the client disconnects from pynixd and queries binary caches instead. The spawned daemon seems to use its default local store rather than pynixd for these queries.

## Testing

**IMPORTANT**: Unix socket stores (--builders unix://...) do NOT work with pynixd for builds. See `ai/minimax/debug_notes.md` for detailed explanation.

**Use SSH stores instead**: When testing with `nix build --builders ssh-ng://... --max-jobs 0`:
- The SSH store uses RemoteStore which properly uses the daemon protocol for all operations
- Use a local tmp store for the nix build invocation so the client runs as local user
- SSH credentials should be configured for passwordless authentication

Example:
```bash
nix build --builders "ssh-ng://localhost?store=/tmp/pynixd-test-0" --max-jobs 0
```