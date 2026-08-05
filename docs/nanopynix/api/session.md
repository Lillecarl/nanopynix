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

.. autofunction:: nanopynix.strip_ansi
```

`get_verbosity` and `set_verbosity` return a `LogLevel` on both engines, so a
caller reads one back whether or not it passes one in. `normalize_log_level`
accepts the wider `LogLevelInput` — an `int` or a level name as well — and is
the way in.

**The level belongs to the object you call it on.** An `EvalSession` follows
its `Session` until it sets a level of its own, and it keeps that level from
then on. So two evaluators of one session can log at two levels, and a later
`Session.set_verbosity` moves only the evaluators that never set one.

Two things stay at the session's level. A `Store` is session-scoped, and it
dispatches through its session. The threads Nix starts for itself, such as a
substituter or a build hook, read a process-wide default that only
`Session.set_verbosity` writes — so an evaluator at `DEBUG` gets debug output
from its own evaluation, and not from those threads.

Nix writes colour into a message when the message is built, and not when the
message is printed. A log event and an error therefore carry the escape
sequences to every consumer. `strip_ansi` removes them. It calls Nix's own
`filter_ansi_escapes`, so it reads the same bytes as an escape sequence that
Nix reads. Do not write a regular expression for this: three of them lived in
this repository before, and each one missed a sequence that Nix removes.
