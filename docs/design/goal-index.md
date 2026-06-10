# Goal Index Design

## Index keys: `DerivedPath | DrvOutput`

### Why DerivedPath

DerivedPath is Nix's universal "I need this to exist" handle. Every query
(BuildPaths, QueryMissing, QueryValidPaths) expresses targets as
DerivedPath variants:

| Variant | Constructor | When indexed |
|---|---|---|
| Built | `DerivedPath::Built(drv, outputs)` | BuildPathsWithResults, QueryMissing |
| Opaque | `DerivedPath::Opaque(path)` | Substitution attempts, valid-path checks |
| BuiltNested | chain of outputs through dynamic derivations | DynamicBuildGoal resolution |

**StorePath aliasing:** An opaque StorePath becomes `DerivedPath::Opaque(sp)`.
When a DerivationBuildGoal discovers its output path, it converts to the
opaque DerivedPath and looks up the index — finds any OpaqueBuildGoal already
registered there — adds itself as a lower-priority alternative. Same target,
two strategies, one entry.

### Why DrvOutput

DrvOutput is `(hash_algo, hash_value, output_name)` — the identity of a
derivation output BEFORE any store path is known. This is the earliest
moment dedup is possible:

- ResolutionGoal: computes store path from DrvOutput via hashDerivationModulo.
  Two build goals that share an input derivation will both need the SAME
  resolution. Indexing on DrvOutput catches this before either has a path.

- CA-floating lookups: query realisations by DrvOutput, before substitution
  produces the store path. Same realisation need → same DrvOutput → same
  index entry.

- Substitution manager queries: `query_realisations(drv_outputs)` uses
  DrvOutput keys natively.

### What is NOT indexed here

- **Parsed Derivation objects** — cached with `@lru_cache` on `read_derivation()`,
  not in the goal index. Not a target, just data.

- **NarInfo / substitution metadata** — cached in the substitution manager's
  internal dicts. Not a target.

- **Raw StorePath** — always wrapped as DerivedPath::Opaque so every entry
  shares the same key type.

### Index expansion pattern

Goals discover more information during execution and register themselves at
newly-discovered keys:

```
1. DerivationBuildGoal at DerivedPath('abc.drv!out')
   → parses .drv → discovers output = /nix/store/xyz-output
   → converts to DerivedPath::Opaque(xyz-output)
   → looks up index → registers as alternative if entry exists
   → FUTURE: any OpaqueBuildGoal for xyz-output finds us here

2. ResolutionGoal at DrvOutput('sha256:abc', 'out')
   → computes hashDerivationModulo → discovers store path
   → converts to DerivedPath::Opaque(path)
   → registers at new key → any future check for that path finds this entry
```

The index starts small and expands as goals run. Two key types cover every
point where a goal transition happens (need → discovered store path).
