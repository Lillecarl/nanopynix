# Session

`Session` is the entry point: it owns one subprocess worker running Nix, and
hands out {doc}`Store <store>` and {doc}`EvalSession <eval>` facades that
share it. `Nix` is a backward-compatible alias for `Session`.

```{eval-rst}
.. autoclass:: nanopynix.rpc.Session
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: nanopynix.rpc.LogCapture
   :members:

.. autoclass:: nanopynix.LogCollector
   :members:

.. autofunction:: nanopynix.normalize_log_level

.. autodata:: nanopynix.LogLevelInput
```
