# Session

`Session` is the entry point: it owns one subprocess worker running Nix, and
hands out {doc}`Store <store>` and {doc}`EvalSession <eval>` facades that
share it. `Nix` is a backward-compatible alias for `Session`.

```{eval-rst}
.. autoclass:: nanopynix.Session
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: nanopynix.LogCapture
   :members:
```
