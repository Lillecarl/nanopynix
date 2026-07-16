# Evaluation

`session.eval(store)` returns an `EvalSession` that holds the worker
exclusively for its duration. Every `ValueProxy` (and the `ValueAttrs`/
`ValueList` views over it) is only valid while its owning `EvalSession` is
open.

```{eval-rst}
.. autoclass:: nanopynix.EvalSession
   :members:
   :undoc-members:

.. autoclass:: nanopynix.ReplSession
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: nanopynix.ValueProxy
   :members:
   :undoc-members:

.. autoclass:: nanopynix.ValueAttrs
   :members:

.. autoclass:: nanopynix.ValueList
   :members:

.. autoclass:: nanopynix.LockedFlakeHandle
   :members:
```
