# 05 — Resolve wrapper derivations with dynamic inputDrvs

**Status**: Not started  
**Depends on**: 01 (DerivedPath), 02 (text-hashed resolution), 03 (trampoline), 04 (DAG linking)  
**Priority**: Medium — only needed when pynixd proxies a `BuildPaths` containing `DrvWithVersion` derivations

## Problem

The `wrapper` derivation uses `DrvWithVersion("xp-dyn-drv",...)` format. Its `inputDrvs` have nested structure:
```
[("producingDrv.drv", ([], [("out", ["out"])]))]
```

This means: "I need the `out` output of the derivation that IS the `out` output of `producingDrv`."

The `wrapper`'s `out` env variable is a **DownstreamPlaceholder**:
```
env["out"] = "/11q8m77b1abq9lpb9x7d57dcj389449a7vfrarhznkgfh51wfy8d"
```

Before building the wrapper, these placeholders must be resolved to actual store paths — similar to how `_resolve_deferred_derivation()` resolves placeholders for deferred derivations.

## Resolution algorithm

1. After `producingDrv` and the inner `hello` derivation are built, we know:
   - `producingDrv`'s `out` = `/nix/store/...-hello.drv` (the inner .drv path)
   - Inner `hello`'s `out` = `/nix/store/...-hello` (the actual output)
2. The wrapper's DownstreamPlaceholder for `producingDrv^out^out` must be replaced with `/nix/store/...-hello`
3. The wrapper's `inputDrv` entries must be resolved: the `([], [("out", ["out"])])` becomes just `input_srcs` entries after resolution
4. The resolved wrapper must be written via `AddToStore(text:sha256)` — same pattern as deferred resolution

## DownstreamPlaceholder computation

Nix's `DownstreamPlaceholder::unknownDerivation()`:
```
placeholder = "/" + nix32_encode(SHA256("nix-upstream-output:" + drvPath.hashPart() + ":" + outputPathName(drvName, outputName)))
```

For nested references like `drv^out^out`, the placeholder is computed from the inner `SingleDerivedPath::Built` chain. Each nesting level adds another layer of hashing.

## Steps

1. Extend `derivation_resolution.py` with `resolve_dynamic_derivation()` for `DrvWithVersion` derivations
2. Compute DownstreamPlaceholders for dynamic output references
3. Replace placeholders in builder, args, env with actual paths from completed trampoline builds
4. Move resolved `inputDrvs` entries into `inputSrcs`
5. Write resolved .drv via `AddToStore(text:sha256)` to both local and builder stores
6. Update `build.request.drv_path` and `build.request.derivation`

## Key difference from deferred resolution

- **Deferred**: placeholder replacement for CA outputs referenced by non-CA derivations
- **Dynamic**: placeholder replacement for nested dynamic output references (`drv^out^out`)
- The placeholder computation is different (unknown-certain-output vs unknown-unknown-output)
- The resolution must be done AFTER the trampoline build completes (we need the inner .drv's output path)

## Verification

The `wrapper` build should succeed through pynixd proxy, producing the same output as direct `nix build`.