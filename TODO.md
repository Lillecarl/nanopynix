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
sites" counted `pynix/src` + `ekn/src` + `docs` when it was measured; `ekn`
has since moved to easykubenix. Not nanopynix's own
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

0. ~~**CRASH: infinite recursion segfaults the process.**~~ -- DONE.
   `let f = n: f (n + 1); in f 0` -- the canonical Nix mistake -- killed
   the interpreter with SIGSEGV on `inproc` and killed the worker
   (`WorkerDiedError: Connection lost`) on `rpc`. Both engines now raise
   `NixError: stack overflow; max-call-depth exceeded`, with Nix's
   `ErrorInfo` intact.

   Cause: the C stack was exhausted *before* Nix's `max-call-depth`
   counter (default 10000) could fire. Nix's default depth needs roughly
   27 MB of C stack; a thread inherits 8 MiB from `RLIMIT_STACK`.

   Fix: the evaluator thread is created with the stack Nix itself asks
   for -- `setStackSize(60 * 1024 * 1024)` = 62914560, a number with no
   setting to read it from. See `NIX_EVALUATOR_STACK_SIZE` in
   `_core/_nix_executor.py`; all three evaluator executors pass it, the
   store pools do not (Nix stores do not recurse).

   The reason we never inherited it is sharper than "the CLI evaluates on
   the main thread": Nix makes that call from the **CLI's `main()`**
   (`src/nix/main.cc:605`), not from `initNix()`. Any embedder of libnix
   is in the same position we were, however it threads.

   **Correcting what this item previously recorded as conclusive.**
   It claimed `threading.stack_size()` "does not reach the eval thread"
   and that the eval probably did not run on the `ThreadPoolExecutor`
   thread. Both are false. Measured with `pthread_getattr_np` called
   *from inside* `ev.run()`: the thread is `nix-eval_0`, the pool thread,
   and its stack is exactly what was requested (1 MiB -> 1048576,
   256 MiB -> 268435456). Two things made the old reading wrong -- a
   `ctypes` probe that let `pthread_self()` default to a 32-bit return,
   truncating the handle; and the fact that `ThreadPoolExecutor` spawns
   lazily inside `submit()`, so the value in force at the *first submit*
   is the one that takes effect, not the value at construction. That is
   why the fix needs `_ensure_worker_spawned`'s warm-up task rather than
   just setting the size in `__init__`.

   Applied as a floor, not an override (`max(size, previous)`), matching
   `nix::setStackSize`, which only ever raises
   (`if (limit.rlim_cur < stackSize)`). A host that wants a raised
   `max-call-depth` can still set `threading.stack_size()` higher and keep
   it. Verified: default depth is clean; `max_call_depth=100000` at 60 MiB
   segfaults, and the same at a host-requested 512 MiB is clean.

   Note we succeed where Nix's own mechanism fails. `setStackSize` raises
   `RLIMIT_STACK` and so cannot exceed the *hard* limit -- 8 MiB on a
   stock host, where the CLI prints that warning and falls back. A pthread
   stack is mmap'd and not bound by `RLIMIT_STACK`.

   **Deliberately not done: `nix::detectStackOverflow()`**
   (`nix/main/shared.hh:96`). It is what still separates us from the CLI
   in one residual case: `nix eval --option max-call-depth 100000` on the
   runaway expression prints "stack overflow (possible infinite
   recursion)" and exits 1, where we segfault. Read
   `src/libmain/unix/stack.cc` before reconsidering; three things there
   decide it, and all three are now confirmed from source rather than
   inferred:

   - `defaultStackOverflowHandler` is literally
     `write(2, "error: stack overflow ...")` then `_exit(1)`. For an
     embedded library that turns "segfault kills the host's Python
     process" into "nicer message, then still kills the host's Python
     process". `stackOverflowHandler` is a replaceable function pointer,
     but replacing it does not change that you are in a signal handler on
     an exhausted stack, where throwing is not available.
   - `detectStackOverflow()` installs a process-wide SIGSEGV `sigaction`,
     which fights `faulthandler` and anything the host set up. The
     handler does hand genuine unrelated faults back (restores `SIG_DFL`
     and returns unless the faulting address is within 4096 bytes of the
     stack pointer), but the `sigaction` is still ours to have taken.
   - The alt stack is the blocker for doing it per-thread. `sigaltstack`
     is per-thread, so it would have to run in `thread_initializer` --
     but the buffer behind it is a single **`static`** `stackBuf`.
     Concurrent `EvalSession`s each own a thread, so they would share one
     alt stack and corrupt each other's handler frames.

   The honest place for it, if ever, is the rpc worker alone -- a process
   already allowed to die, whose death is already surfaced as
   `WorkerDiedError` -- and only with a per-thread alt-stack buffer of
   our own rather than Nix's shared static.

3. ~~**Bare `RuntimeError` escapes the `NixError` hierarchy.**~~ -- DONE,
   both causes and all four symptoms. `attr()` on a
   non-attrs value and `list_get()` on a non-list were incidentally fixed
   by item 1 (they raise Nix `TypeError` now), and `to_python()`'s
   max-call-depth error is fixed below. `realise_string()` on a derivation
   was on this list in error: measured, it succeeds and returns the store
   path, and on a non-string it already raises `NixTypeError`. Remaining:
   ~~`attr_get`'s "attribute not found"~~ -- DONE, together with
   `list_get`'s out-of-range, because they are the attrset and list halves
   of one question and answering them differently would be the worst
   outcome. Both are now Python exceptions **and** Nix ones:
   `MissingAttributeError(EvalError, KeyError)` and
   `ListIndexError(EvalError, IndexError)`.

   The deciding test was "can Python's exceptions carry everything Nix
   has here?", and they can, because an exception may belong to two
   hierarchies -- the same pattern `EvalHashMismatchError(EvalError,
   HashMismatchError)` already uses. So `except KeyError` works on an
   attrset as it does on a dict, `except NixError` still works, `.key` /
   `.index` are machine-readable, and `info["suggestions"]` carries Nix's
   own "Did you mean ...?" ranking -- computed in C++ from the attrset's
   symbol table, which is the part a bare Python `KeyError` could not
   have had.

   One wart, fixed with an explicit `__str__`: left to the MRO,
   `KeyError.__str__` renders `repr(args[0])`, so every message would
   have arrived wrapped in quotes.

   Deliberate asymmetry worth remembering: the *bound* classes
   (`nanopynix_bindings.errors.ListIndexError`) are not `IndexError`s.
   Pythonic-ness is a property of the public class that boundary-A
   translation produces, consistent with the bound classes having no
   relationship to the public hierarchy at all.

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

   - ~~**Plain `nix::Error` is deliberately not registered**
     (`nix_util.cpp:323`), and we throw it ourselves in
     `nix_expr.cpp`~~ -- DONE, both halves.

     *Our own throws*: the conversion from plain `nix::Error` to
     `nix::EvalError` landed with the single-translator work below, not
     here -- every site is already converted (`nix_expr.cpp:321,330,333`
     for `edit_location`, `:485,488,508,511` for `derived_path`). What was
     left open here was that nothing *pinned* it. Verified
     from Python, not just read: `edit_location()` on a function raises
     `nanopynix.exceptions.EvalError` with `info` intact. Matching Nix
     was explicitly *not* the tiebreaker -- Nix throws plain
     `nix::Error` for the analogous conditions (`nix edit`'s
     `findPackageFilename`, installables' "expected a derivation")
     because `Error` is its default, not because it judged the
     category. `EvalError` is the honest answer: these are all
     eval-domain errors about a `Value`.

     Pinned by
     `test_exception_translation.py::test_our_own_binding_throws_pick_a_registered_subclass`,
     added because a regression here is **silent**: reverting a throw to
     plain `nix::Error` still yields a `NixError` with intact
     `ErrorInfo` via the catch-all, so only the class coarsens and
     nothing else would notice. Its teeth come from the contrast with
     the test above it -- the *same method* on an int gives base
     `NixError` (Nix's own throw) and on a function gives `EvalError`
     (ours).

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
     `tests/nanopynix/test_exception_translation.py`: the catch-all fires; it
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

4. ~~**`get_derived_path` is inproc-only with zero consumers.**~~ -- DONE,
   deleted outright. It was public on inproc and absent on rpc, which the
   parity ledger recorded as a defect; the two ways to settle that were to
   add it to rpc or to stop having it, and nothing needed it.

   `build()` -- its only non-test caller -- takes the DerivedPath from L1
   inline, since it is the only caller. The tests that wanted the
   intermediate path use `attr("drvPath")`, which is what any caller would
   reach for and is byte-identical (measured, not assumed: same store path
   both ways). They need the path rather than `build()` because they build
   in batches of 50 to measure how many builds Nix dispatches concurrently,
   which building one at a time would destroy.

   Made private first, then deleted: private still keeps the method and the
   parity question alive, and neither was earning its place. The `^output`
   selection-syntax question is moot until someone has a real use for the
   string, at which point it goes on *both* engines.

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

6. ~~**`force_as` (8 sites), `get_type`/`type` (12), `close`, and the view
   classes.**~~ -- DONE, in two commits as required. `force_as` deleted
   (it was `as_int` with a worse rule: it rejected an int for `FLOAT`
   where Nix's own `forceFloat` widens), inproc's `type() -> str` folded
   into `get_type() -> NixType`, `close()` deleted as a bare alias for
   `release()`, `ValueAttrs`/`ValueList` deleted, and rpc `call()`'s
   client-side `WrongNixTypeError` pre-check removed so Nix does the
   rejecting.

   **This item's own prescription was wrong, and the fix is the
   opposite one.** It said `force()` "should return `NixType` on both
   engines, keeping Nix's verb." Measured while implementing it:
   *learning the type already forces*, on both engines --
   `_worker_eval.py`'s `_do_type_name` calls `value.force()` before
   `value.type_name()`, and rpc's `_ensure_type` wraps that. So
   `force() -> NixType` would have been `get_type()` under a second
   name: this item would have finished by creating the exact
   duplication it exists to remove.

   So `force()` is **deleted**, not retyped. It had no unique job left
   on either engine, and was in fact a duplicate of two *different*
   methods depending on which engine you were on:
   - inproc's was `value.force(); value.to_python()` -- a deep
     conversion wearing WHNF's name, i.e. exactly `to_python()`.
   - rpc's was `_ensure_type()` plus a view wrapper, i.e. exactly
     `get_type()` plus the classes being deleted.

   To force for effect (to make something raise), call `get_type()` and
   ignore the answer. That is also how Nix reads: `forceValue` returns
   void and the caller inspects `value->type()` afterwards -- the verb
   is never the goal.

   `NixValue` (`ValueProxy | ValueAttrs | ValueList | JsonValue`) went
   with them: it existed only as `force()`'s return type and had no
   other referent once the union collapsed.

   The 66 call sites were migrated by reading each one, not by pattern
   -- deliberately, because the same source text `await v.force()`
   needed *opposite* replacements per engine on compound values
   (`to_python()` on inproc, `as_dict()`/`as_list()` on rpc). Retired
   with them: the `force_attrs`/`force_list` semantic-ledger entries
   (the divergence was entirely about what a forced compound returns, a
   question neither engine is asked any more), `TestValueListBounds`
   (`as_list()` returns a real Python list, so the bounds check and the
   "no RPC for an impossible index" guarantee are Python's own), and
   the `ValueAttrs` borrowing-view lifetime tests, rewritten to assert
   the better contract the lazy children give: the parent is pinned
   only until a child resolves and takes a handle of its own.

   **Left dead by this and not yet removed: the `Force` RPC itself.**
   `eval.proto`'s `rpc Force`, `ForceRequest`, and `common.proto`'s
   `ForceValue` now have no client. See item 10.

## Deferred -- the only item with real semantic migration cost

7. ~~**`as_dict() -> dict[str, Value]` / `as_list() -> list[Value]`**~~ --
   DONE, and promoted out of "deferred ergonomics" because it turned out to
   be the answer to item 9 rather than a nicety.

   Keys and length now, contents still lazy -- which is exactly WHNF for a
   compound value as Nix models it, since forcing an attrset leaves its
   values as thunks. `as_` and not `to_` for the documented reason: an
   attrset already *is* a mapping of names to values, so this hands it over
   rather than converting anything.

   The `as_`/`to_` split stays load-bearing: `as_*` means "this already is
   that, hand it over" and never converts, `to_python` means "convert, with
   Nix's `toJSON` rules" and is lossy on purpose. Renaming `as_int` to
   `to_int` would imply `to_int()` on `"42"` works.

   Cost was far lower than the 32-site estimate suggested, because nothing
   had to be migrated -- this is purely additive, and no existing caller
   changes. Over rpc it needed **zero new wire ops**: `attr_names()` gives
   the keys and `attr()` already returns a lazy proxy that makes no call
   until forced, so `as_dict()` is one round trip regardless of width. On
   inproc it is one dispatch onto the Nix thread for the whole level
   (`_attr_values`), not one per attribute.

## Open design question, found while doing item 5

9. ~~**nanopynix's own bundled primops return attrsets that
   `to_python()` refuses.**~~ -- DONE, and the resolution is neither option
   the item proposed. Both were wrong because both assumed the primop's
   shape was the problem.

   It is not. `.address n` and `.subnet n d` *have* to be functions -- a /8
   has 16 million addresses, so `.address` cannot be a list -- and
   `to_python()` refusing an attrset containing a function is correct, since
   `nix eval --json` refuses the same thing. What was actually missing was
   any way to read a value *one level at a time*, with data leaves and
   function leaves side by side. That is item 7's `as_dict()`, now done, and
   it closes this without touching the primop's documented interface:

       entries = await network.as_dict()
       await entries["prefixlen"].to_python()          # 24
       await (await five.apply(entries["address"])).to_python()

   Rejected on the way, recorded so it is not re-proposed: splitting the
   callables under a `.fn` key. It only shrinks the caller's `removeAttrs`
   list from two names to one, invents a shape to appease a conversion that
   is right to refuse, and changes a documented interface to do it.

   Also rejected, and worth being explicit about because it looks
   attractive: teaching `to_python()` to emit callables for function leaves.
   That means re-introducing a hand-rolled deep traversal, which is exactly
   the code `nix_expr.cpp:826-836` records deleting -- it was
   "a reimplementation of `printValueAsJSON` minus every rule that makes it
   terminate" and took SIGSEGV on any derivation, whose `out`/`all`/
   `drvAttrs` point back at itself. `printValueAsJSON` owns the termination
   rules (`__toString`, `outPath`, max-call-depth); a custom walk has to
   re-own all of them. The cost is termination, not transport -- functions
   already cross the wire fine via `attr()`/`apply()`.

   Recorded in `tests/nanopynix/primops/test_ipaddress_primops.py`'s
   `test_the_callables_are_reachable_through_as_dict`, on the primop's own
   docstring, and for both engines in the semantic parity matrix.

## Dead wire surface, left by item 6

10. ~~**The `Force` RPC has no client.**~~ -- DONE. Deleting
    `ValueProxy.force()` left `eval.proto`'s
    `rpc Force(ForceRequest) returns (nix.common.ForceValue)` reachable
    only by the worker handler answering it. Removed: the rpc, its
    `ForceRequest`, `common.proto`'s `ForceValue` message, the
    `models.py` re-export, and `_worker_eval.py`'s
    `force`/`_do_force`/`_force_handle`. Every remaining read goes
    through `AsScalar` (scalars), `ForceJson` (deep),
    `Attr`/`ListGet`/`AttrNames`/`ListLength` (navigation), or
    `TypeName` (`get_type`).

    Same shape as item 5's `ForceDeep` deletion and the first CIP's
    `daemon.proto` finding: a schema kept alive only by the code that
    serves it. Kept out of item 6's commit because it touches the proto
    and so the generated-module build.

    `DeepValue`/`DeepList`/`DeepAttrs` were checked at the same time and
    are **not** dead despite `ForceDeep` being gone -- `manager.proto`
    carries primop call args and results as `DeepValue`. Left alone.

## Verification

8. ~~**`test_engine_parity.py` compares signatures**, so "same name, same
   signature, different behaviour" passes.~~ -- DONE.
   `tests/nanopynix/test_engine_parity_semantics.py` is the other half:
   `(expression, operation)` run on both engines, outcomes compared as
   either a returned value or an exception *type*. 25 success cases, 19
   failure cases.

   It found a divergence on its first run, which is the point. rpc's
   `attr()`/`list_get()` are sync and lazy, so `{ x = 1; }.attr("nope")`
   hands back an unresolved proxy where inproc raises
   `MissingAttributeError` immediately. The paired `*_forced` cases show
   the engines agree once the value is forced -- so it is *when* the error
   arrives, not whether. Recorded in `SEMANTIC_LEDGER` with the same
   discipline as the signature `LEDGER`: an entry asserts the engines
   still disagree, so it cannot outlive what it documents. Item 6's
   unification of `attr` should delete both entries.

   `tests/temp/` is gone. `test_exception_translation.py` moved to
   `tests/nanopynix/` unchanged; the matrix moved to
   `test_error_boundaries.py`, keeping the three invariants the semantic
   layer does not cover (store/build failures across both backends,
   `NixError`-catchability, and `nix::ErrorInfo` compared field by field
   across boundaries A and B) and dropping the JSON side-file recorder in
   favour of pytest-agent notes. Its `repo_root`/`nixpkgs_path` fixtures
   were duplicated in three conftests; they now live once in
   `tests/conftest.py`.

# Tracked: pre-existing complexity/arg-count debt (ruff-strict C901/PLR09xx)
Enabling mccabe (`C901`) and Pylint's too-many-{branches,returns,arguments,
statements} (`PLR0911`/`PLR0912`/`PLR0913`/`PLR0915`) in `ruff-strict.toml`
surfaced 39 pre-existing hotspots above their thresholds, each suppressed
with a `# noqa` pointing here rather than refactored blind as part of the
lint-rule rollout (a refactor risks behavior changes; a lint sweep shouldn't
bundle them). Worth tackling opportunistically when next touching one of
these functions: `tests/support/lsp_scenario.py`'s `_apply` (38 branches/87 statements),
`nanopynix_testing.nix_runtime`'s `pytest_collection_modifyitems` (21
branches/56 statements), `pynix/_lsp/_syntax.py`'s `_resolve_declaration`
(18 branches) and `_identifier_path_at_node` (17 branches/11 returns), and a
long tail of smaller too-many-arguments constructors/factories (`ekn/apply.py`,
`nanopynix_helpers/build.py`, `nanopynix/inproc/_impl.py`,
`rpc/client/_pool.py`, `rpc/client/session.py`, `rpc/client/_session.py`,
among others). Run `ruff check --select C901,PLR0911,PLR0912,PLR0913,PLR0915`
for the full current list.
