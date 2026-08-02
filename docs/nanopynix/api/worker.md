# Worker (L3)

The top-level `nanopynix` API runs Nix in an isolated worker subprocess and
communicates with it over gRPC. `Session` owns the worker and provides its
`Store` and `EvalSession` facades.

```{toctree}
:maxdepth: 1

session
store
eval
```

```{eval-rst}
.. automodule:: nanopynix.rpc
```
