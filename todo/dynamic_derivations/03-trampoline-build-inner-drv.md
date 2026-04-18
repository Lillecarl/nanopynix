# 03 — Dynamic derivation trampoline (build inner .drv from text-hashed output)

**Status**: Not started  
**Depends on**: 01 (DerivedPath), 02 (text-hashed resolution)  
**Priority**: High — this is the core "dynamic" behavior

## Problem

After building a text-hashed CA derivation (e.g., `producingDrv`), its output IS a `.drv` file. The next step is to **build that inner derivation** — this is the "trampoline" pattern from Nix's `DerivationTrampolineGoal`.

Currently pynixd has no mechanism to discover that a build output is a `.drv` file and automatically schedule its inner derivation for building.

## The trampoline flow

```
Client requests: BuildPaths([producingDrv.drv^out^out])
                                    │
                          ┌─────────┘
                          ▼
              1. Build producingDrv (text-hashed CA)
                          │
                          ▼
              2. Output is /nix/store/...-hello.drv
                          │
                          ▼
              3. Parse this output AS a .drv file
                          │
                          ▼
              4. Build the inner hello derivation
                          │
                          ▼
              5. Return hello's output path
```

## Implementation approach

### Option A: Scheduler-level trampoline

In `execute_build()`, after a successful build, check if any output ends in `.drv` AND `has_dynamic_outputs` is true. If so, parse the inner .drv and enqueue a new build.

### Option B: BuildPaths-level decomposition

When `BuildPaths` receives a `DerivedPath` with nested references (e.g., `producingDrv^out^out`), decompose it into:
1. Build `producingDrv^out` (the text-hashed output)
2. After step 1 completes, parse the output as a .drv
3. Build the inner derivation's `out` output

This is more aligned with Nix's `DerivationTrampolineGoal` approach.

### Recommendation

Option B — handle decomposition at the `BuildPaths` level where we already decompose into `BuildDerivation` requests. For nested `DerivedPath` references, create a two-phase build sequence.

## Steps

1. In `_decompose_build_paths()`, detect `DerivedPath` entries with nested references
2. Create phase-1 builds for the outer derivations (text-hashed CA producers)
3. After phase-1 completes, parse outputs as .drv files
4. Enqueue phase-2 builds for the inner derivations
5. Link DAG dependencies between phases

## Key data flow

After building `producingDrv`:
```python
# The output path is known from the BuildDerivationResponse
producing_out = realisation["outPath"]  # e.g., /nix/store/...-hello.drv

# Read this output from the local store filesystem
inner_parsed = read_drv_file(local_store.store_path, StorePath(producing_out))

# The inner .drv has input_srcs that need to be present
# These are the same input_srcs from producingDrv (Nix makes them available)
inner_basic = to_basic_derivation(inner_parsed, local_store.store_path)

# Enqueue a BuildDerivation for the inner .drv
inner_req = BuildDerivationRequest(drv_path=StorePath(producing_out), derivation=inner_basic)
```

## Verification

```bash
# Build producingDrv^out^out through pynixd proxy
python tests/ai/dynamic_drv.py  # Step 5 should work through proxy
```