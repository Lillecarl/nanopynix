# Session

`Session` is the entry point: it owns one subprocess worker running Nix, and
hands out {doc}`Store <store>` and {doc}`EvalSession <eval>` facades that
share it.

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

.. autoclass:: nanopynix.LogLevel
   :members:
   :undoc-members:
```

`get_verbosity` and `set_verbosity` return a `LogLevel` on both engines, so a
caller reads one back whether or not it passes one in. `normalize_log_level`
accepts the wider `LogLevelInput` — an `int` or a level name as well — and is
the way in.
