# Store

`Store` is the ergonomic facade for one opened Nix store, returned by
`session.store()`. The complete generated request/response RPC API remains
available through `store.rpc` for operations without a dedicated method yet.

```{eval-rst}
.. autoclass:: nanopynix.Store
   :members:
   :undoc-members:

.. autoclass:: nanopynix.StoreHandle
   :members:
```
