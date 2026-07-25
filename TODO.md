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

# Tracked: the value-reading API, measured

Ordered by measured cost, not by how appealing the change is. "Consumer
sites" counts `pynix/src` + `ekn/src` + `docs` only -- not nanopynix's own
layers (each method spans ~6: protocol, proto, worker, client, inproc,
binding) and not tests.

The evidence is a matrix of 10 input shapes (int/string/bool/null/attrs/
list/function/derivation/`throw`/lazy-`throw`) x 15 methods x both engines.
Regenerate it before acting on anything here; the numbers below are from
the run right after `to_python()` moved onto `printValueAsJSON`.

## Defects -- zero consumer sites, fix independently of any naming

1. **`attr_names`/`has_attr`/`list_length` answer for the wrong type
   instead of raising**, identically on both engines. `attr_names()` on
   `42` is `[]`; `has_attr("a")` on `42` is `False`; `list_length()` on
   `{ a = 1; }` is `0`. So `for i in range(await v.list_length())` is a
   silent no-op on an attrset -- a plausible answer, not an exception,
   which is the worst failure mode an API can have.

2. **rpc's `force_deep` still dies on a derivation.** The walk was
   implemented three times: `to_python()` in C++ (fixed -- it delegates to
   `printValueAsJSON`), `_deep_value()` in `rpc/worker/_worker_eval.py`
   (a separate Python-level recursion that never calls `to_python()`, so
   the fix did not reach it), and `printValueAsJSON` itself. rpc raises
   where inproc now returns the store path.

3. **Bare `RuntimeError` escapes the `NixError` hierarchy** on inproc from
   `attr()` on a non-attrs value, `list_get()` on a non-list,
   `realise_string()` on a derivation, and `to_python()`'s
   max-call-depth cycle error.

4. **`get_derived_path` is inproc-only with zero consumers.** The parity
   ledger already calls it `"DEFECT: extracting a DerivedPath is pure
   libexpr."` `build_paths_with_results` already accepts
   `Sequence[str | PublicStorePath]`, so DerivedPath construction belongs
   in the build receiver. Check the `^output` selection syntax first.

## Consolidation -- mechanical, unique names, `sed`-able

5. **`force_deep` / `force_json` / inproc's `json()` are one operation**
   (~63 consumer sites). `force_json` and `json()` are both
   `printValueAsJSON`; `force_deep` is a broken duplicate whose only extra
   -- function leaves stay callable -- is rpc-only, since inproc used to
   return the *string* `"function"` for them. Keep one, as
   `to_python(*, copy_to_store=False)`: it returns Python objects, not
   JSON, and "json" names the ruleset, which belongs in the docstring.
   `copy_to_store` decides what a *path value* becomes -- `true` copies
   the source into the store as string interpolation does, `false`
   renders the literal filesystem path as `nix eval --json` does.

6. **`force_as` (8 sites), `get_type`/`type` (12), `close`, and the view
   classes.** `force_as(INT)` is `as_int` with a worse rule: it rejects an
   int for `FLOAT` where Nix's own `forceFloat` widens. `get_type` and
   `type` also differ in *return* -- `NixType` enum vs a plain string.
   `close()` duplicates `release()`. `ValueAttrs`/`ValueList` have zero
   consumers outside their own unit tests and are the sole reason
   `force()` diverges; `force()` should return `NixType` on both engines,
   keeping Nix's verb.

## Deferred -- the only item with real semantic migration cost

7. **`as_dict() -> dict[str, Value]` / `as_list() -> list[Value]`** (32
   consumer sites, each needing thought rather than a regex). Keys and
   length now, contents still lazy -- which is exactly WHNF for a compound
   value as Nix models it, since forcing an attrset leaves its values as
   thunks. They would subsume `attr_names`/`has_attr`/`list_length` and
   let plain `dict`/`list` supply `len`/`in`/`.keys()`/iteration for free.
   Deliberately *not* bundled with item 1: once those three raise, the
   defect is closed and this is judged on ergonomics alone.

   The `as_`/`to_` split is load-bearing and should survive: `as_*` means
   "this already *is* that, hand it over" and never converts, `to_python`
   means "convert, with Nix's `toJSON` rules" and is lossy on purpose.
   Renaming `as_int` to `to_int` would imply `to_int()` on `"42"` works.

## Verification

8. **`test_engine_parity.py` compares signatures**, so "same name, same
   signature, different behaviour" passes. It missed every divergence
   above. Seed a semantic layer from the matrix: `(expression, operation)`
   -> same result or same exception type on both engines.

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
