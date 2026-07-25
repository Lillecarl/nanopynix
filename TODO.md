# TODO

# Deferred: debatable-tier "magic value" findings
The magic-values audit also turned up ~15 lower-confidence findings that
were deliberately left alone in this pass (not clear violations, more a
matter of taste/context): `LogCollector`'s `maxsize=10_000`, cosmetic
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

# Resolved: `apply()` replaced the coercion family

Recorded because the reasoning is worth not re-deriving, and because it is the
rule to apply to the next helper someone wants to add.

The engine-parity ledger once listed twelve entries for two accessor families.
They turned out to be different operations, not one concept spelled two ways,
and they were resolved in opposite directions:

- `as_int`/`as_float`/`as_bool`/`as_string` are the FFI boundary -- producing an
  actual Python object is the one thing no Nix expression can do. Added to rpc
  so both engines have them, with the type check running in the worker so both
  raise the same `NixTypeError`.
- `coerce_str`/`coerce_int`/`coerce_float`/`coerce_bool` were **deleted**.
  `coerce_str` was `builtins.toString` reimplemented in Python, and wrong with
  it (`"true"` where Nix says `"1"`, `"null"` where Nix says `""`, no handling
  of `__toString`/`outPath`/lists at all). The other three had no Nix
  counterpart -- Nix has no `toInt`/`toFloat`/`toBool` -- so there was nothing
  to be faithful to.

`Value.apply(function)` replaced them: a Nix function expression, or an
already-evaluated function value, applied to the value. It reimplements
nothing, reaches every builtin rather than four hand-picked conversions, and
needed no new wire surface -- it is `eval.string()` plus the existing `call()`.
Passing an evaluated function is also the memoisation story, without `apply`
owning a cache.

The rule that follows: **if Nix can already express it, expose the door, not a
copy of the room.** A helper that wraps a builtin has to justify itself against
`apply("builtins.thatOne")`, and a helper with no Nix counterpart has to
justify existing at all.

# Tracked: pre-existing complexity/arg-count debt (ruff-strict C901/PLR09xx)
Enabling mccabe (`C901`) and Pylint's too-many-{branches,returns,arguments,
statements} (`PLR0911`/`PLR0912`/`PLR0913`/`PLR0915`) in `ruff-strict.toml`
surfaced 39 pre-existing hotspots above their thresholds, each suppressed
with a `# noqa` pointing here rather than refactored blind as part of the
lint-rule rollout (a refactor risks behavior changes; a lint sweep shouldn't
bundle them). Worth tackling opportunistically when next touching one of
these functions: `tests/support/lsp_scenario.py`'s `_apply` (38 branches/87 statements),
`tests/support/nix_runtime.py`'s `pytest_collection_modifyitems` (21
branches/56 statements), `pynix/_lsp/_syntax.py`'s `_resolve_declaration`
(18 branches) and `_identifier_path_at_node` (17 branches/11 returns), and a
long tail of smaller too-many-arguments constructors/factories (`ekn/apply.py`,
`nanopynix_helpers/build.py`, `nanopynix/inproc/_impl.py`,
`rpc/client/_pool.py`, `rpc/client/session.py`, `rpc/client/_session.py`,
among others). Run `ruff check --select C901,PLR0911,PLR0912,PLR0913,PLR0915`
for the full current list.
