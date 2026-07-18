# Requests & Responses

All Nix daemon protocol operations, organized by op code. Each operation module defines a `*Request` (subclass of `WireRequest`) and a `*Response` (subclass of `WireResponse`).

Operations marked with `forward = False` are intercepted by pynixd's build DAG and never forwarded to the upstream daemon. Extensions (`is_extension = True`) are pynixd-specific additions beyond the standard Nix daemon protocol.

---

## Standard Operations

### Op 1 — IsValidPath

```{eval-rst}
.. automodule:: pynixd.serde.is_valid_path
   :members:
```

---

### Op 6 — QueryReferrers

```{eval-rst}
.. automodule:: pynixd.serde.query_referrers
   :members:
```

---

### Op 7 — AddToStore (streaming request)

```{eval-rst}
.. automodule:: pynixd.serde.add_to_store
   :members:
```

---

### Op 9 — BuildPaths

`forward = False`

```{eval-rst}
.. automodule:: pynixd.serde.build_paths
   :members:
```

---

### Op 10 — EnsurePath

```{eval-rst}
.. automodule:: pynixd.serde.ensure_path
   :members:
```

---

### Op 11 — AddTempRoot

```{eval-rst}
.. automodule:: pynixd.serde.add_temp_root
   :members:
```

---

### Op 12 — AddIndirectRoot

```{eval-rst}
.. automodule:: pynixd.serde.add_indirect_root
   :members:
```

---

### Op 14 — FindRoots

```{eval-rst}
.. automodule:: pynixd.serde.find_roots
   :members:
```

---

### Op 19 — SetOptions

```{eval-rst}
.. automodule:: pynixd.serde.set_options
   :members:
```

---

### Op 20 — CollectGarbage

```{eval-rst}
.. automodule:: pynixd.serde.collect_garbage
   :members:
```

---

### Op 23 — QueryAllValidPaths

```{eval-rst}
.. automodule:: pynixd.serde.query_all_valid_paths
   :members:
```

---

### Op 26 — QueryPathInfo

```{eval-rst}
.. automodule:: pynixd.serde.query_path_info
   :members:
```

---

### Op 29 — QueryPathFromHashPart

```{eval-rst}
.. automodule:: pynixd.serde.query_path_from_hash_part
   :members:
```

---

### Op 31 — QueryValidPaths

```{eval-rst}
.. automodule:: pynixd.serde.query_valid_paths
   :members:
```

---

### Op 32 — QuerySubstitutablePaths

```{eval-rst}
.. automodule:: pynixd.serde.query_substitutable_paths
   :members:
```

---

### Op 33 — QueryValidDerivers

```{eval-rst}
.. automodule:: pynixd.serde.query_valid_derivers
   :members:
```

---

### Op 34 — OptimiseStore

```{eval-rst}
.. automodule:: pynixd.serde.optimise_store
   :members:
```

---

### Op 35 — VerifyStore

```{eval-rst}
.. automodule:: pynixd.serde.verify_store
   :members:
```

---

### Op 36 — BuildDerivation

`forward = False`

```{eval-rst}
.. automodule:: pynixd.serde.build_derivation
   :members:
```

---

### Op 37 — AddSignatures

```{eval-rst}
.. automodule:: pynixd.serde.add_signatures
   :members:
```

---

### Op 38 — NarFromPath (streaming response)

```{eval-rst}
.. automodule:: pynixd.serde.nar_from_path
   :members:
```

---

### Op 39 — AddToStoreNar (streaming request)

```{eval-rst}
.. automodule:: pynixd.serde.add_to_store_nar
   :members:
```

---

### Op 40 — QueryMissing

```{eval-rst}
.. automodule:: pynixd.serde.query_missing
   :members:
```

---

### Op 41 — QueryDerivationOutputMap

```{eval-rst}
.. automodule:: pynixd.serde.query_derivation_output_map
   :members:
```

---

### Op 42 — RegisterDrvOutput

```{eval-rst}
.. automodule:: pynixd.serde.register_drv_output
   :members:
```

---

### Op 43 — QueryRealisation

```{eval-rst}
.. automodule:: pynixd.serde.query_realisation
   :members:
```

---

### Op 44 — AddMultipleToStore (streaming request)

```{eval-rst}
.. automodule:: pynixd.serde.add_multiple_to_store
   :members:
```

---

### Op 45 — AddBuildLog

```{eval-rst}
.. automodule:: pynixd.serde.add_build_log
   :members:
```

---

### Op 46 — BuildPathsWithResults

`forward = False`

```{eval-rst}
.. automodule:: pynixd.serde.build_paths_with_results
   :members:
```

---

### Op 47 — AddPermRoot

```{eval-rst}
.. automodule:: pynixd.serde.add_perm_root
   :members:
```

---

## Pynixd Extensions

### Op 101 — PynixdCollectGarbage

```{eval-rst}
.. automodule:: pynixd.serde.pynixd_collect_garbage
   :members:
```

---

### Op 103 — QueryPathInfos

```{eval-rst}
.. automodule:: pynixd.serde.query_path_infos
   :members:
```

---

### Op 104 — QueryClosure

```{eval-rst}
.. automodule:: pynixd.serde.query_closure
   :members:
```

---

### Op 105 — QueryClosureWithInfo

```{eval-rst}
.. automodule:: pynixd.serde.query_closure_with_info
   :members:
```

---

### Op 106 — QueryDerivationOutputMapBatch

```{eval-rst}
.. automodule:: pynixd.serde.query_derivation_output_map_batch
   :members:
```

---

### Op 107 — SignPathInfo

```{eval-rst}
.. automodule:: pynixd.serde.sign_path_info
   :members:
```

---

### Op 108 — ProbeSystems

```{eval-rst}
.. automodule:: pynixd.serde.probe_systems
   :members:
```

---

### Op 109 — ProbeFeatures

```{eval-rst}
.. automodule:: pynixd.serde.probe_features
   :members:
```
