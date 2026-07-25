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

1. ~~**`attr_names`/`has_attr`/`list_length` answer for the wrong type
   instead of raising**~~ -- DONE. All five navigation accessors now force
   through `forceAttrs`/`forceList`, so a wrong type raises Nix's own
   `TypeError` instead of `[]`/`False`/`0`. The estimate of zero consumer
   sites was wrong by one: `pynix/derivation.py` used `has_attr("type")`
   as a type probe and now checks `get_type()` explicitly.

2. ~~**rpc's `force_deep` still dies on a derivation.**~~ -- DONE with
   item 5; the third walk (`_deep_value()`) is deleted.

0. **CRASH: infinite recursion segfaults the process.**
   `let f = n: f (n + 1); in f 0` -- the canonical Nix mistake -- kills the
   interpreter with SIGSEGV on `inproc` and kills the worker
   (`WorkerDiedError: Connection lost`) on `rpc`. `nix eval` on the same
   expression prints `error: stack overflow; max-call-depth exceeded` and
   exits cleanly.

   Measured cause: the C stack is exhausted *before* Nix's `max-call-depth`
   counter (default 10000) can fire. Bisected on `inproc` with
   `NixEvalSettings(max_call_depth=N)`: clean `RuntimeError` at
   N<=4000, SIGSEGV at N>=6000. Running the *same* default-depth
   expression under `ulimit -s 262144` raises cleanly -- so it is stack
   size, not the counter.

   Why the CLI survives and we don't: Nix raises `RLIMIT_STACK` at startup
   (`nix::setStackSize`, `nix/util/current-process.hh`) and evaluates on
   the main thread. Measured: `RLIMIT_STACK` is `8388608` both before and
   after our `init_nix()`, so nanopynix never gets that. We also evaluate
   on a `ThreadPoolExecutor` thread, whose stack is fixed at creation.

   `threading.stack_size()` does **not** reach the eval thread, so the
   obvious Python-side lever is not available. Two measurements, together
   conclusive: a 1 MiB thread stack still survives 4000 frames (<=262
   B/frame), while the 8 MiB default already dies at 6000 (>1398 B/frame)
   -- inconsistent by more than 5x; and 6000 frames crash identically at
   requested stack sizes of 1 MiB, 256 MiB, and default, a 256x range with
   no effect at all. Whatever thread Nix evaluates on, its stack tracks the
   process `RLIMIT_STACK`, not the `threading` module setting. Find out why
   before picking the fix -- most likely the eval is not on the
   `ThreadPoolExecutor` thread that `_nix_executor.py` appears to run it
   on, which would be worth knowing on its own.

   `nix::detectStackOverflow()` (`nix/main/shared.hh`) is the other half of
   a fix: it installs a SIGSEGV handler so an overflow is *reported*
   ("stack overflow (possible infinite recursion)") rather than silent.
   Read before relying on it -- in the Lix fork of the same function
   (`lix/libmain/stack.cc:44-63`, the only copy present in the store; the
   CppNix 2.34 source we actually build against was not available to
   check) it pairs a process-wide `sigaction` with a **per-thread**
   `sigaltstack`, which would mean it has to run on the eval thread and
   not merely at init. Supporting observation: `python -X faulthandler`
   printed nothing on the crash, consistent with a handler that cannot run
   because the stack is exhausted and no alt stack is installed.
   `ulimit -s 262144` fixing it is the load-bearing evidence, not this.

3. **Bare `RuntimeError` escapes the `NixError` hierarchy.** `attr()` on a
   non-attrs value and `list_get()` on a non-list were incidentally fixed
   by item 1 (they raise Nix `TypeError` now), and `to_python()`'s
   max-call-depth error is fixed below. `realise_string()` on a derivation
   was on this list in error: measured, it succeeds and returns the store
   path, and on a non-string it already raises `NixTypeError`. Remaining:
   `attr_get`'s own `throw std::runtime_error("attribute '...' not found")`
   -- that last one is a *different* question from wrong-type (missing key,
   not bad type) and needs a deliberate answer: `NixError` or `KeyError`?
   Note `list_get` out of range already raises `IndexError`
   (`std::out_of_range`), so `KeyError` is the symmetric answer. Don't fold
   it in silently; `test_attr_get_missing_raises` currently pins
   `RuntimeError`, and the cycle test in
   `test_scalar_accessor_semantics.py` deliberately matches only the
   message with a comment saying it should tighten when this is fixed.

   Two *causes* behind this, both measured, both wider than the four
   symptoms above:

   - ~~**`nix::EvalBaseError` is not registered.**~~ -- DONE. It is the
     parent of `EvalError`, and Nix derives three errors from it directly
     rather than from `EvalError`: `StackOverflowError` (max-call-depth),
     `IFDError`, and `RecoverableEvalError`
     (`nix/expr/eval-error.hh:49-80`). All three reached Python as a bare
     `RuntimeError` with **no `ErrorInfo` at all**. Now registered before
     `EvalError` in the same translation unit, and mapped to the existing
     public `EvalError` -- Nix's reason for the split is cacheability,
     which no Python caller can act on, so it earns no new public type.

   - **Plain `nix::Error` is deliberately not registered**
     (`nix_util.cpp:323`), and we throw it ourselves in
     `nix_expr.cpp` at lines 318, 330, 468, 471, 491, 494 -- so
     `edit_location()` and `get_derived_path()` on a wrong-typed value
     produce a bare `RuntimeError`, measured. Two halves:

     *Our own throws* are easy: pick a registered subclass. Note that
     matching Nix here is not the tiebreaker it usually is -- Nix throws
     plain `nix::Error` for the analogous conditions (`nix edit`'s
     `findPackageFilename`, installables' "expected a derivation")
     because `Error` is simply its default, not because it judged the
     category. `EvalError` is the honest answer: these are all
     eval-domain errors about a `Value`.

     ~~*Nix throwing a bare `nix::Error` from libstore/libexpr
     internals*~~ -- DONE, and it dissolved the constraint rather than
     working around it. All the exception classes and **one** translator
     now live in `nanopynix-bindings/src/nix_errors.cpp`; with a single
     translator owning the hierarchy there is no registration order left
     to get wrong, so `nix::Error` can finally be the catch-all. The
     ordering that remains is the `catch` chain's, which is in one place
     and is checked by the compiler (`-Wexceptions` rejects a base
     preceding its subclass) rather than by convention.

     Two rejected alternatives, recorded so they are not re-proposed:
     relying on `nanopynix_bindings/__init__.py`'s import order (one
     line, but silently breakable), and an ordering-independent fallback
     that rethrows for each specific type before `catch (nix::Error &)`
     (correct, but duplicates the registry in a second list that must be
     kept in sync).

     Three things that design must keep doing, all pinned by
     `tests/temp/test_exception_translation.py`: the catch-all fires; it
     does not shadow the specific subclasses; and it does not overreach
     -- standard C++ exceptions (`std::bad_alloc`, nlohmann-json,
     our own `std::out_of_range` in `list_get`) have no clause and must
     keep falling through to nanobind's default translator. A
     `catch (...)` would silently destroy the third.

     Also handled there: `nix::Interrupted` is a `BaseError` sibling of
     `nix::Error`, not a subclass, and now maps to `KeyboardInterrupt`
     instead of degrading to a bare `RuntimeError`. Nix warns about
     exactly this in `nix/util/error.hh` -- "BaseError should generally
     not be caught, as it has Interrupted as a subclass" -- so the
     catch-all must stay rooted at `Error`, never `BaseError`.

4. **`get_derived_path` is inproc-only with zero consumers.** The parity
   ledger already calls it `"DEFECT: extracting a DerivedPath is pure
   libexpr."` `build_paths_with_results` already accepts
   `Sequence[str | PublicStorePath]`, so DerivedPath construction belongs
   in the build receiver. Check the `^output` selection syntax first.

## Consolidation -- mechanical, unique names, `sed`-able

5. ~~**`force_deep` / `force_json` / inproc's `json()` are one
   operation**~~ -- DONE. All three collapsed into
   `to_python(*, copy_to_store=False)` on both engines, plus the
   `ForceDeep` RPC, `ForceDeepRequest`, and `NixDeepValue` deleted. The
   wire op stays `ForceJson` because it really does transfer JSON.
   `copy_to_store` decides what a *path value* becomes -- `true` copies
   the source into the store as string interpolation does, `false`
   renders the literal filesystem path as `nix eval --json` does.

   **This surfaced an unresolved design tension, see item 9.**

6. **`force_as` (8 sites), `get_type`/`type` (12), `close`, and the view
   classes.** `force_as(INT)` is `as_int` with a worse rule: it rejects an
   int for `FLOAT` where Nix's own `forceFloat` widens. `get_type` and
   `type` also differ in *return* -- `NixType` enum vs a plain string.
   `close()` duplicates `release()`. `ValueAttrs`/`ValueList` have zero
   consumers outside their own unit tests and are the sole reason
   `force()` diverges; `force()` should return `NixType` on both engines,
   keeping Nix's verb.

   Split this into at least two commits. Deleting the view classes changes
   `force()`'s *return type* on rpc -- the largest behavioural change left
   in this list -- and does not belong in the same commit as renaming
   `get_type`.

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

## Open design question, found while doing item 5

9. **nanopynix's own bundled primops return attrsets that
   `to_python()` refuses.** `builtins.parseNetwork` ships `.address n`
   and `.subnet n d` as Nix *functions* in its result attrset -- part of
   its documented interface -- and `parseInterface` nests one of those
   under `.network`. Nix will not convert a function to JSON, so neither
   will `to_python()`, and a caller who does the obvious thing gets an
   error whose fix is a Nix expression they have to know to write
   (`builtins.removeAttrs`, which is what
   `tests/nanopynix/primops/test_ipaddress_primops.py` now does).

   Raising is the *correct* answer for `to_python()` -- the old deep walk
   only "worked" over rpc, and produced the useless string `"function"`
   in-process, so the engines disagreed. The unresolved part is the primop
   design: we shipped an interface that our own flagship conversion
   rejects. Options, neither chosen: split the data and the accessors
   (e.g. `.fn.address`, leaving the top level plain data), or keep the
   shape and document it on the primop. Do not leave the `removeAttrs` in
   the test as the only record of the problem.

## Verification

8. **`test_engine_parity.py` compares signatures**, so "same name, same
   signature, different behaviour" passes. It missed every divergence
   above. Seed a semantic layer from the matrix: `(expression, operation)`
   -> same result or same exception type on both engines.

   Do this **before** item 6, not after: the matrix is the seed, item 6
   changes three more of the behaviours it measures, and landing 6 first
   means writing the parity tests against a moving target twice.

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
