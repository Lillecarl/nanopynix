# 07 — Fix QueryRealisation wire format

**Status**: Not started  
**Priority**: Low — cosmetic, not blocking dynamic derivation progress

## Problem

`QueryRealisationRequest` for text-hashed CA derivations fails with:
```
BackendError: Daemon error (Error): unknown hash algorithm '/nix/store/wsfdgkf6905rn06lq7x5445i594a1j88', expect 'blake3', 'md5', 'sha1', 'sha256', or 'sha512'
```

The daemon is parsing a store path as a hash algorithm. The actual bug is likely that `QueryRealisationRequest.to_writer()` sends a bare store path instead of the `drvPath!outputName` format the daemon expects for realisation IDs.

## Note on ANSI escapes

The daemon's error messages may include ANSI escape codes when connected to a pty-like stream. This is normal Nix daemon behavior — stderr is interleaved with the protocol response. It's not a pynixd bug.

## Steps

1. Check `QueryRealisationRequest.to_writer()` — verify the realisation ID format matches what the daemon expects
2. Check `QueryDerivationOutputMapRequest` for comparison (that one works for text-hashed CA)
3. Fix the wire format and verify