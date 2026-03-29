# pynixd BuildDerivation Debugging - Investigation Update

## Current Understanding

Based on research into Nix source code and Rio-build implementation:

### How Remote Builds Should Work

1. **Client sends BuildDerivation** to pynixd
2. **pynixd builds** on remote builder, stores outputs in local store
3. **pynixd returns BuildResult** with output paths (status=0)
4. **Client's daemon queries outputs** via:
   - QueryPathInfo - check if outputs exist
   - NarFromPath - get the actual NAR data
   - AddToStoreNar - import into client's local store

### What We're Seeing

- BuildDerivation returns status=0 (success)
- QueryPathInfo returns valid path (pynixd has it)
- But client still gets "builder for ...drv failed on remote builder"
- This error means `remoteError` was set in derivation-goal.cc:944

### Key Finding: "Nix tries to BUILD AGAIN"

From debug notes: "Nix tries to BUILD AGAIN!"

This means after BuildDerivation succeeds, nix is trying to REBUILD the derivation. This happens when:
1. The build result is not properly recognized
2. The output paths don't match what nix expects
3. There's a validation failure somewhere

## Possible Issues

### 1. BuildResult Format Issue
The BuildResult might not be properly formatted - missing or incorrectly encoded built_outputs.

Looking at `operations/base.py`:
```python
@dataclass
class BuildResult:
    status: int = 0
    error_msg: str = ""
    # ... other fields
    built_outputs: dict[str, dict] = field(default_factory=dict)
```

The built_outputs map drvOutput to Realisation JSON. Need to verify this is correctly serialized.

### 2. Output Path Not Being Retrieved
The client might not be calling NarFromPath, or might be calling it incorrectly.

### 3. Store Path Mismatch
The paths returned in BuildResult might not match what the client expects.

## Next Steps

1. **Verify BuildResult serialization** - ensure built_outputs is properly encoded
2. **Check what paths client queries** - trace exactly what paths are requested after BuildDerivation
3. **Verify NarFromPath handling** - ensure pynixd correctly serves NARs for built outputs
4. **Check AddToStore** - ensure client can import into its local store

## Research Files

- `nix_remote_build_research.md` - How Nix remote builds work
- `rio_build_research.md` - Rio-build implementation details
- `debug_notes.md` - Previous debugging notes