# 06 — BuildDerivation wire format for DrvWithVersion

**Status**: Deferred  
**Depends on**: 05 (resolution — resolved derivations won't need this)  
**Priority**: Low — if we always resolve before sending, this isn't needed

## Problem

When pynixd sends a `BuildDerivation` request to a builder daemon, it serializes a `BasicDerivation` using `to_writer()`. Currently this always produces the `Derive(...)` ATerm format.

If the derivation is dynamic (`is_dynamic=True`), the ATerm should use `DrvWithVersion("xp-dyn-drv",...)` format. However, pynixd should **always resolve** dynamic derivations before sending `BuildDerivation` (task 05), meaning the resolved derivation no longer has `inputDrvs` entries and can be sent as regular `Derive(...)`.

## When this matters

Only if we want to send an **unresolved** dynamic derivation to a builder daemon for it to resolve locally. This would require:
- Protocol >= 1.36 on the builder (the daemon must understand `DrvWithVersion`)
- The builder must also support `dynamic-derivations` experimental feature
- All dynamic dependency outputs must be available on the builder

## Recommendation

**Skip for now.** Always resolve dynamic derivations before building (task 05 handles this). This task becomes relevant only if we want to optimize by letting the daemon resolve instead of pynixd.