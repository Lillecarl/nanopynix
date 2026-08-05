# Store

`Store` is the ergonomic facade for one opened Nix store, returned by
`session.store()`. The complete generated request/response RPC API remains
available through `store.rpc` for operations without a dedicated method yet.

```{eval-rst}
.. autoclass:: nanopynix.rpc.Store
   :members:
   :undoc-members:

.. autoclass:: nanopynix.rpc.StoreHandle
   :members:
```

## A bare `.drv` means every output here

`query_missing` and `build_paths_with_results` take *derived paths*, in Nix's
`^` notation. A `Store` reads a plain `.drv` as every output of that
derivation, so these two ask the same thing:

```python
await store.build_paths_with_results([drv])
await store.build_paths_with_results([f"{drv}^*"])
```

**This is not Nix's own reading, and the difference is deliberate.** To Nix a
bare `.drv` is `DerivedPath::Opaque` — "make this path present" — so
`nix build <drv>` builds none of its outputs and reports success having done
nothing. `query_missing` on the same argument then reports nothing to build,
which reads as "already up to date" for a derivation that was never built.

{meth}`~nanopynix.DerivedPath.for_build` is the conversion, and each engine's
`Store` applies it before the request reaches a binding.
`nanopynix_bindings` therefore keeps Nix's meaning: a bare `.drv` there is
opaque, and a caller of the compiled bindings can still ask for the fetch.

Everything else passes through unchanged. A bare path that is *not* a
derivation stays an opaque fetch, because that is what it genuinely means, and
a string that already carries `^` said what it wanted.

## Running programs from a relocated store

A store opened with a root (`local://?root=…`, `unix://…?root=…`) reports
ordinary logical `/nix/store/…` paths while the bytes live under that root.
Such a path is not executable where it says it is: the ELF interpreter,
`DT_RUNPATH`, shebangs and every store reference baked into the closure all
name the *logical* directory. `store_exec_prefix` returns an argv prefix that
makes the store real at that location for the duration of one command, the way
`nix run` does.

It returns an empty list for an ordinary store, so prepend it unconditionally
rather than branching on the store's layout:

```python
prefix = await nanopynix.store_exec_prefix(store)
await anyio.run_process([*prefix, f"{out}/bin/tofu", "version"])
```

```{eval-rst}
.. autofunction:: nanopynix.store_exec_prefix

.. autodata:: nanopynix.STORE_EXEC_TOOL
```
