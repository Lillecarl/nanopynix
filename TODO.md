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

# Deferred: pydantic models for ekn's fixed-schema Nix-JSON inputs
Fixing ekn's remaining pyright errors retyped raw Nix/YAML/kr8s JSON data
from `Any`/`object`/`dict[str, Any]` to `nanopynix.models.JsonValue` (and a
new `ekn.apply.Manifest = dict[str, JsonValue]` alias), which resolves the
type erasure with isinstance-narrowing + a few small per-item validation
helpers (`gitops.py`'s `_as_manifest_list`, `sops.py`'s `_as_str_list`).
That's the proportionate fix for genuinely open-ended k8s manifest dicts
(arbitrary Kind, no fixed schema). Two of the retyped inputs, though, DO
have a small fixed schema: `kubernetes.sopsAgeIdentities` entries
(`sops.py`'s `ensure_age_identities`) and `kubernetes.gitOpsTargets` entries
(`gitops.py`'s `resolved_targets`). Upgrading those specifically to real
pydantic models (matching the rest of `eval.py`'s config models) would
replace the manual isinstance/raise validation with declarative schema
validation and clearer error messages -- a real improvement, but a
separate, behavior-changing redesign rather than a type-error fix, so it
wasn't bundled into this pass.
