# RPC log request finalization

`pynix build --update-fod` needs the Nix fixed-output hash mismatch that the
worker reports through the log stream. A worker RPC response can arrive before
the corresponding log event reaches the client, so a `LogCapture` cannot safely
stop at the response boundary today.

Design this with request-scoped logging rather than a timing workaround:

- Allocate and propagate request IDs through the multi-threaded worker to the
  Nix thread-local logger request ID.
- Associate each client RPC operation and its emitted log events with that ID.
- Emit an ordered request-finalized log marker after the worker operation has
  completed, analogous to Nix daemon protocol's `STDERR_LAST` boundary.
- Let `LogCapture` wait for that marker before yielding its final event set.

This enables reliable per-request log filtering as well as deterministic FOD
hash-update handling. It deliberately requires RPC/protocol design work and is
out of scope for the repository reorganisation.

Affected tests:

- `tests/pynix/test_build.py::test_build_update_fod_rewrites_and_rebuilds`
- `tests/pynix/test_build.py::test_build_update_fod_rewrites_each_named_run_command_dependency`
