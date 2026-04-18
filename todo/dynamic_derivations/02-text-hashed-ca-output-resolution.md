# 02 — Text-hashed CA derivation output resolution

**Status**: Not started  
**Depends on**: 01 (DerivedPath nested refs)  
**Priority**: High — text-hashed outputs are the "gateway" to dynamic derivations

## Problem

When `pynixd` builds a **text-hashed CA derivation** (like `producingDrv` with `outputHashMode="text"`), the output path is unknown at evaluation time (it's a floating CA output). After building, the output IS a `.drv` file at a content-addressed path.

Currently pynixd's `_resolve_deferred_derivation()` in `scheduler.py` only handles `OutputKind.DEFERRED` (non-CA derivations depending on CA derivations). Text-hashed outputs have a different resolution flow: they're CA floating outputs where the hash method is `text`.

## What producesDrv looks like

```python
# Parse result:
outputs: [('out', '', 'text:sha256', '')]  # path='', floating
is_dynamic: False  # The producingDrv itself is NOT dynamic
# It's a plain CA derivation with text hashing

# After building, its output path is e.g.:
# /nix/store/sgjsm7kig5na7n81q042glq59gdj0d03-hello.drv
# (which IS a valid .drv file)
```

## Resolution flow for text-hashed outputs

1. Build `producingDrv` — the daemon produces the text-hashed output
2. `BuildDerivationResponse.result.built_outputs` contains the realisation with the output path
3. Register the realisation via `RegisterDrvOutputRequest` on local + builder stores
4. The output path is now a valid `.drv` — parse it with `read_drv_file()`
5. The inner derivation's outputs need to be built (the "trampoline" — see task 03)

## Steps

1. In `scheduler.py:_resolve_deferred_derivation()`, extend to also handle `is_text_hashed` outputs (method=`text`, hash_algo set, path empty)
2. After building a text-hashed derivation, parse the output as a `.drv` file
3. Register the inner derivation's realisation on builder stores
4. Add the inner derivation's outputs to `build.required_paths`
5. Ensure `QueryDerivationOutputMap` resolves text-hashed output paths correctly

## Key difference from DEFERRED resolution

- **DEFERRED**: Non-CA derivation depends on CA output. Resolve placeholders in the deferred .drv, compute output paths, rewrite the .drv via AddToStore.
- **Text-hashed**: CA derivation whose output IS a .drv. No placeholder rewriting needed for the producingDrv itself — just build it and register the realisation. The inner .drv needs to be built separately.

## Verification

```bash
python tests/ai/dynamic_drv.py  # Step 2-3 should show buildingDrv output path resolved
```