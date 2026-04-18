# 04 — Dynamic dependency DAG linking in decomposition

**Status**: Not started  
**Depends on**: 01 (DerivedPath), 03 (trampoline)  
**Priority**: High — without this, builds execute in wrong order

## Problem

`_decompose_build_paths()` in `build_paths.py` builds DAG edges by walking `parsed.input_drvs` (line 167). But for `DrvWithVersion("xp-dyn-drv",...)` derivations, the relevant dependencies are in `parsed.dynamic_input_drvs`, not `parsed.input_drvs`.

Example: the `wrapper` derivation has:
```python
input_drvs: {}  # EMPTY — no direct input derivations
dynamic_input_drvs: {producingDrv.drv: {"out": ["out"]}}  # depends on dynamic output
```

Without walking `dynamic_input_drvs`, the `wrapper` build has NO DAG edges and will be scheduled before `producingDrv` is built, causing a failure.

## Steps

1. In `_decompose_build_paths()`, after the existing `input_drvs` DAG linking loop, add a second loop that walks `dynamic_input_drvs`
2. For each `(drv_path, child_map)` in `dynamic_input_drvs`:
   - Look up the build_id for `drv_path` in `drv_to_build_id`
   - Add `depends_on` edges from the current build to that build
3. Also handle the trampoline case: if a build depends on a dynamic output `drv^out`, it needs to depend on both:
   - The build that produces the outer `.drv` file (the text-hashed CA derivation)
   - The build that produces the inner derivation's output (the trampoline result)
4. Consider: should `drv_to_build_id` map include entries for nested paths (e.g., `drv_path^out`)?

## Data structure change

Current `drv_to_build_id: dict[str, int]` maps `str(drv_path) -> build_id`. For dynamic deps, we need to map `str(drv_path) + "^" + output_name` to a trampoline build ID.

## Verification

The `wrapper` build should only start after `producingDrv` AND the inner `hello` derivation have both completed.