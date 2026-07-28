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
