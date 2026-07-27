# Evaluation

`session.eval(store)` returns an `EvalSession` that holds the worker
exclusively for its duration. Every `ValueProxy` -- including the lazy
children handed back by `as_dict()` and `as_list()` -- is only valid while its
owning `EvalSession` is open.

```{eval-rst}
.. autoclass:: nanopynix.rpc.EvalSession
   :members:
   :undoc-members:

.. autoclass:: nanopynix.rpc.ReplSession
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: nanopynix.rpc.ValueProxy
   :members:
   :undoc-members:

.. autoclass:: nanopynix.rpc.LockedFlakeHandle
   :members:
```
