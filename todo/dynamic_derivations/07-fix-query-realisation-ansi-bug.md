# 01 — Fix QueryRealisation ANSI escape bug

**Status**: Not started  
**Blocks**: Verification of dynamic derivation realisation queries  

## Problem

`QueryRealisationRequest` for text-hashed CA derivations fails with:
```
BackendError: Daemon error (Error): unknown hash algorithm '/nix/store/wsfdgkf6905rn06lq7x5445i594a1j88', expect 'blake3', 'md5', 'sha1', 'sha256', or 'sha512'
```

The daemon response includes ANSI color escape codes (e.g., `\x1b[35;1m`) around the drv path, indicating the pynixd reader isn't stripping them properly. The daemon's error messages include ANSI formatting when connected to a terminal-like stream.

## Steps

1. Investigate `QueryRealisationRequest.to_writer()` — verify the drv output string format matches what the daemon expects (`drvPath!outputName`, not a bare drv path)
2. Check if the daemon's stderr is leaking ANSI codes into the response stream
3. Check if `QueryRealisationResponse.from_reader()` is reading the wrong fields or mis-parsing
4. Fix and verify with `tests/ai/dynamic_drv.py` Step 8

## Verification

```bash
python tests/ai/dynamic_drv.py  # Step 8 should succeed
```