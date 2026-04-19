# Dynamic Derivations — Task Breakdown

Experimental Nix feature: derivations whose outputs are themselves derivations (`.drv` files).

## Task Dependency Graph

```
01 (DerivedPath nested refs)
├── 02 (Text-hashed CA output resolution)
│   └── 03 (Trampoline: build inner .drv)
│       └── 04 (Dynamic dep DAG linking)
│           └── 05 (Resolve wrapper dynamic inputs)
└── 07 (Fix QueryRealisation ANSI bug) [independent]
    06 (BuildDerivation DrvWithVersion wire) [optional/low priority]
```

## Execution Order

| # | Task | Priority | Depends on | Status |
|---|------|----------|------------|--------|
| 01 | Extend DerivedPath for nested `^out^out` refs | Critical | — | DONE |
| 02 | Text-hashed CA output resolution | High | 01 | DONE |
| 03 | Trampoline: build inner .drv from text-hashed output | High | 01, 02 | DONE |
| 04 | Dynamic dep DAG linking in decomposition | High | 01, 03 | DONE |
| 05 | Resolve wrapper derivations with dynamic inputDrvs | Medium | 01-04 | DONE |
| 06 | BuildDerivation wire format for DrvWithVersion | Low | 05 (skip if always resolving) | Deferred |
| 07 | Fix QueryRealisation ANSI escape bug | Medium | — (independent) | Pending |

## Research Artifacts

- `test-dyn-drv.nix` — Nix expressions for dynamic derivation test fixtures
- `tests/ai/dynamic_drv.py` — Research script that exercises the full lifecycle
- `todo/test_ca_ops.md` — CA derivation research (prerequisite, already done)
- `todo/deferred_build_ordering.md` — Deferred derivation resolution (prerequisite, already done)

## Key Concepts

- **Text-hashed CA derivation**: `outputHashMode="text"`, output IS a `.drv` file
- **DrvWithVersion("xp-dyn-drv",...)**: ATerm format for derivations with dynamic dependencies
- **dynamic_input_drvs**: `{drv_path: {output_name: [nested_output_name]}}` — nested `childMap`
- **DownstreamPlaceholder**: placeholder path for unresolved dynamic outputs (like `/11q8m...`)
- **Trampoline pattern**: build outer .drv, parse output as inner .drv, build inner .drv
- **SingleDerivedPath::Built**: nested reference like `drv^out^out`

## Nix Protocol Notes

- Client must advertise `dynamic-derivations` feature to daemon (protocol >= 1.36)
- `BuildPaths` accepts `DerivedPath::Built` with nested `^` references
- No new daemon ops — dynamic derivations use existing BuildPaths/BuildDerivation
- After resolution, dynamic derivations become regular `Derive(...)` (no `DrvWithVersion`)