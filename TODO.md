# TODO

# inproc drops FINALIZED log events
`inproc/_impl.py`'s `_forward_logs` only handles `LogStreamEventKind.NIX`
events (`if kind != LogStreamEventKind.NIX: continue`), silently dropping
`LogStreamEventKind.FINALIZED` events -- whereas the RPC-based worker
(`rpc/worker/_worker.py`) and daemon (`rpc/daemon/_worker.py`) log-forwarding
paths both explicitly handle `FINALIZED` too. Investigate whether this is
intentional (inproc callers may have no use for the finalized-boundary
signal) or a real gap.

# Deferred: debatable-tier "magic value" findings
The magic-values audit also turned up ~15 lower-confidence findings that
were deliberately left alone in this pass (not clear violations, more a
matter of taste/context): the 64 KiB read-buffer size in
`rpc/daemon/_connection.py`, `LogCollector`'s `maxsize=10_000`, cosmetic
thread-name-prefix literals, `generate_slug(2)`, the `store_workers: int = 4`
default, `rpc/client/_session.py`'s hardcoded store-handle-`1` default
(implicitly tied to `HandleRegistry._next`'s start value), and
`session.py`'s uncoordinated `anyio.fail_after(60)` shutdown deadline.
Revisit if/when a stronger convention or a concrete bug motivates it.
