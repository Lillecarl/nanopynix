# `ekn deploy`/`ekn eval` performance: logger firehose (fixed) + eval-time stall (open)

Investigation started from `hetzkube`'s `pynix ekn deploy --file . -A kubenix
--push` consistently failing after ~5-15 minutes with `DEADLINE_EXCEEDED`,
never reaching git commit/push. `EKN_TIMING=1` staging showed the time was
burned before `Deploy` even reached the commit stage, inside
`Validate`/`internal.manifestJSONFile` evaluation.

## Fixed: `PyLogger` was forwarding hundreds of thousands of dead events

`nanopynix-bindings/src/nix_util.cpp`'s `PyLogger` (the custom `nix::Logger`
that bridges Nix's C++ callbacks to Python) was forwarding almost every
`Activity` start/stop/result Nix generates during a big build's closure
realization -- for this one hetzkube deployment, **432,000+ events for a
single `.build()` call**, each paying a full `gil_scoped_acquire` + Python
callback + `janus.Queue` push + (eventually) gRPC/protobuf/pydantic round
trip. Nix's own default logger (`SimpleLogger` in `nix/src/libutil/logging.cc`)
does none of this work for the same events -- `stopActivity` is an empty
virtual by default, and `startActivity` only renders when
`lvl <= verbosity && !s.empty()`.

Verified nothing in this repo ever reads what was being dropped:

- `pynix/src/pynix/_util.py`'s `_forward_nix_logs` explicitly
  `continue`s past every `"stop"` event and every `"result"` event whose
  `result_type` isn't one of the two build-log types.
- `ekn/src/ekn/eval.py`'s `_print_log_event` only renders `action in
  {"msg","warn","error"}` (see `LogEventExt.message_without_ansi`) or
  `result_type` names containing `"BUILD_LOG"`.

Three changes to `PyLogger` (`nanopynix-bindings/src/nix_util.cpp`), all
uncommitted in the working tree:

1. `stopActivity` now does nothing (no consumer anywhere, at any verbosity).
2. `result()` now only forwards `resBuildLogLine`/`resPostBuildLogLine` --
   every other `ResultType` (`resProgress`, `resSetExpected`, `resFileLinked`,
   `resUntrustedPath`, `resCorruptedPath`, `resSetPhase`, `resFetchStatus`) is
   dropped before the GIL, not just the two progress types the prior fix
   already handled.
3. `startActivity` now also requires `!s.empty()`, exactly matching
   `SimpleLogger::startActivity`'s own condition. Sampling showed the bulk of
   forwarded activities were content-free container nodes
   (`actRealise`/`actBuilds`/`actCopyPaths` with `s=""` and `fields=[]`, one
   triplet per store path in the closure) that even Nix's own CLI renders
   nothing for.

Verified end to end: re-running the identical `.build()` call against
`kubenix.config.internal.manifestJSONFile` after rebuilding
(`direnv exec . <cmd>` from hetzkube, which picks up the local-path-overridden
`nanopynix-bindings`/`pynix`) went from 432,000+ forwarded log events to
**zero**, confirmed with a tally subscriber across the whole build.

`direnv exec .` is required to test C++ changes -- `nanopynix-bindings` is a
compiled extension, not part of the editable `pynixDevEnv` set, so it needs a
real Nix rebuild each time, unlike the pure-Python packages.

### Not yet done: symmetric handling for consumers that *do* want activity events

`pynix build --print-build-logs`/`pynix eval --print-build-logs` (a
different, still-standalone CLI path, not `ekn`) does render `"start"` events
via `_forward_nix_logs`'s `else` branch. `ekn eval`/`ekn deploy` never expose
that flag at all (`Eval` in `ekn/src/ekn/cli.py` has no verbosity/
print-build-logs fields), so for the `ekn` codepath every `"start"` event
that still gets through the `lvl`/`s.empty()` filters is also queued for a
consumer that doesn't exist. Only fixed the zero-consumer cases
(`stopActivity`, non-buildlog `result`) plus the upstream-matching
`s.empty()` filter on `startActivity` -- did **not** add an opt-in
"activity forwarding" toggle gating `startActivity` on whether the caller
actually wants build-log visibility, independent of `nix::verbosity`. That
would need a new parameter threaded from `Session()`/`install_logger()`
(currently called unconditionally, once per worker process, in
`nanopynix/src/nanopynix/rpc/worker/_worker.py:415`) down into `PyLogger`'s
constructor. Worth doing if the eval-stall below turns out to still produce a
large `"start"` volume once fixed.

## Open: the same build still stalls, and it's not `.build()` that's slow

After the logger fix, the identical build still takes minutes and can still
hit the 300s gRPC `_RPC_TIMEOUT` (`nanopynix/src/nanopynix/rpc/client/_pool.py:51`).
Critically, the traceback showed the `DEADLINE_EXCEEDED` originating from
**`select_attr` → `has_attr` → `_ensure_resolved`**
(`nanopynix-helpers/src/nanopynix_helpers/eval_target.py:20` →
`nanopynix/src/nanopynix/rpc/client/_session.py:790,480`), walking
`kubenix.config.internal.manifestJSONFile` -- **before `.build()` is ever
called**. The secondary `close_store` `DEADLINE_EXCEEDED` seen in the same
traceback is just session-teardown cleanup failing after the primary
exception, not a separate bug.

Checked the C++ `has_attr`/`attr_names` bindings directly
(`nanopynix-bindings/src/nix_expr.cpp:313-329`) -- both are correctly lazy,
iterating `v->attrs()` for symbol-name membership only, never calling
`forceValue` on any attribute's value (only `attr_get`, line 331, does that,
correctly). So this is not a nanopynix binding bug.

That means merely forcing `kubenix.config` (or an intermediate attrset on the
way to `internal.manifestJSONFile`) to WHNF -- enough to know its attribute
*names*, not read any option's value -- is triggering real
`actRealise`/`actBuilds`/`actCopyPaths` store activity (confirmed via a
tally-by-type sample, see below). In Nix's module system that only happens
if something forces evaluation across a conditional
(an `assertions` entry, a `mkIf` condition, an option's `apply`/type-check)
that itself depends on a built derivation -- e.g. an assertion reading a
built package's output to decide a boolean. Have not yet identified which of
the composed modules is responsible:

- `easykubenix/easykubenix/assertions.nix` (always included)
- hetzkube's `kubenix/modules/*.nix`, `kubenix/full/*.nix`,
  `kubenix/capi/*.nix`, `kubenix/configuration/*.nix` (see
  `hetzkube/kubenix/default.nix`'s module list)

**Next step**: bisect by temporarily commenting out modules from
`hetzkube/kubenix/default.nix`'s `modules = [ ... ]` list (or from
`easykubenix/default.nix`'s own list, starting with `assertions.nix`) and
re-running the repro script below after each removal, to find which module's
`config`/`assertions`/`mkIf` forces a derivation build merely by being
evaluated to WHNF.

### Ruled out

- **Remote substituter network calls**: initially suspected (hetzkube's
  substituters are `cache.nixos.org`/two cachix caches), but `ss -tapx`
  scoped to the worker's actual PID showed exactly one socket, connected to
  the *local* `/nix/var/nix/daemon-socket/socket` -- zero real network
  sockets. (`/proc/PID/net/tcp` is **not** process-scoped without a private
  netns and gave a false positive earlier in this investigation -- use `ss
  -tapx` or resolve `/proc/PID/fd/*` socket inodes instead.) Re-ran with
  `NIX_CONFIG="substitute = false"` and it was not meaningfully faster,
  confirming this isn't substituter-driven.
- **Cross-arch `pkgsOff` cost**: real but separate, ~5 minutes of extra
  eval/deploy time from `pkgsOff` being a full second nixpkgs fixpoint
  evaluation. Fixed in `hetzkube/kubenix/modules/cheapam.nix` (different
  repo, also uncommitted) by gating `pkgsOff.cheapam` behind a new
  `crossArch.enable` option, default off.
- **GIL/logger overhead**: the fix above; confirmed via before/after event
  tallies (432k+ → 0 events for the identical build).

### Baseline

Plain `nix-build --no-out-link -A kubenix.config.internal.manifestJSONFile`
(real Nix CLI, parallel builds via `--max-jobs`) completes in **~2m39s**.
The nanopynix worker, even post-logger-fix, took longer than that and still
hit the 300s deadline in some runs -- consistent with the worker's
`NixThreadExecutor` being a single dedicated thread per
`nanopynix/src/nanopynix/rpc/worker/_worker.py:406`'s own docstring, so all
store operations for a huge closure serialize through one thread/connection
with no parallelism, unlike `nix-build`'s worker-goal concurrency. This may
compound with whatever eval-time forcing is described above, or may be a
separate, secondary cost once the forcing bug is found -- not yet
distinguished.

## Repro script

Bypasses `ekn`'s CLI entirely, using `nanopynix.Session` directly so timing
isn't confused by `ekn`'s own stage wrapping. Run via `direnv exec .` from
`hetzkube` (needed for any `nanopynix-bindings` rebuild to take effect):

```python
import asyncio, time
from nanopynix import NixSettings, Session
from nanopynix.primops import yaml_primops
from nanopynix_helpers.eval_target import select_attr

async def main():
    async with Session(settings=NixSettings(), verbosity="error", primops=yaml_primops()) as session:
        async with session.store() as store, session.eval(store) as eval_:
            root = await (await eval_.file(".")).auto_call()
            t0 = time.monotonic()
            proxy = await select_attr(root, "kubenix.config.internal.manifestJSONFile")
            print(f"select_attr (has_attr walk only): {time.monotonic() - t0:.1f}s")
            t0 = time.monotonic()
            result = await proxy.build()
            print(f"build: {time.monotonic() - t0:.1f}s -> {result}")

asyncio.run(main())
```

The first `select_attr` line printing a large elapsed time (before `build()`
is even reached) is exactly the reproduction to confirm/deny once a
candidate module is found and removed/fixed.

## Uncommitted working-tree state (this repo)

- `nanopynix-bindings/src/nix_util.cpp`: the three `PyLogger` fixes above.
  **Needs review + a fresh `direnv exec . nix build` to confirm it still
  builds clean before committing** (last verified build was mid-session).
- `ekn/src/ekn/cli.py`, `ekn/src/ekn/eval.py`: unrelated `EKN_TIMING`-gated
  `timed_stage()` instrumentation added around `Deploy.run()`'s three stages
  and `_validation_config`'s per-attr builds -- useful for narrowing down
  which stage is slow in a real `ekn deploy` run; harmless when
  `EKN_TIMING` is unset.
- `nix/dev-env.nix`, `default.nix`, `nix/shell.nix`: unrelated fix, makes the
  editable `pynixDevEnv` bake `toString (../pkg + "/src")` instead of reading
  `$NANOPYNIX_GIT_ROOT`, so no env var export is needed for the editable
  install to resolve. Already tested working.

## Uncommitted working-tree state (hetzkube repo)

- `kubenix/modules/cheapam.nix`: adds `crossArch.enable` (default off)
  gating `pkgsOff.cheapam`, per the ruled-out cross-arch cost above. Real,
  independent fix, not yet committed.

## RESOLVED: the eval-time stall root cause (2026-07-24)

Found and fixed. **Not a nanopynix bug at all** -- a laziness leak in
`easykubenix`'s own Nix code, reproducible with plain `nix eval` (no
nanopynix/ekn involved).

### Root cause

`easykubenix/easykubenix/internal.nix` declared:

```nix
options.internal = lib.mkOption { type = lib.types.anything; };
```

`lib.types.anything`'s `merge` (nixpkgs `lib/types.nix`) recurses into every
nested attrset value (via `(attrsOf anything).merge`) to detect
`mkIf`/`mkOverride`/`mkMerge` markers on **each key**, even when there is
only one module definition. That per-key check
(`mergeDefinitions`/`modules.nix:1230`: `!(isAttrs d.value && d.value ?
_type)`) requires WHNF-forcing every key's definition value. WHNF of a
plain attrset (including a not-yet-built derivation) is normally free --
but `internal.nix`'s `manifestYAML`/`manifestYAMLList` keys are defined as
`builtins.readFile manifestYAMLFile`, and `readFile`'s "WHNF" is **not**
free: the primop eagerly realises the file right there. So merely asking
`internal` for its attribute *names* (exactly what `select_attr`'s
`has_attr` walk does, via nanopynix's `attr_get` C++ binding resolving each
path segment) forced `manifestYAMLFile` to build, which (via
`manifestJSON`/`generatedOrdered`'s sort needing every object's `.kind`)
cascaded into building **every** helm release's `chart2yaml` derivation and
every `importyaml` spec's YAML-parsing IFD across the whole module tree --
before `.build()` was ever called, before any option value was read.

Confirmed with a plain `nix eval --impure --json --expr 'builtins.attrNames
(import ./default.nix {}).kubenix.config.internal'` against the real
hetzkube tree (no nanopynix, no ekn): 2m32.7s, 40+ `yaml2json.drv` builds
plus a `manifest.yaml.drv` build, matching the ~2m39s real-nix-build
baseline almost exactly -- all spent computing 11 attribute *names*.

Isolated with a minimal `evalModules` repro using `throw`-instrumented
derivation-shaped values (cache-proof, unlike wall-clock timing): a flat
single-definition attrset with a `types.anything`-typed option was
**safe** (didn't reproduce) -- the bug specifically needs a value nested
inside that's an eager (non-lazy-WHNF) primop call like `readFile`, not
just "any attrset containing a derivation". Confirmed exact stack trace
pinpointing `mergeDefinitions`'s `isAttrs d.value && d.value ? _type` check
forcing `builtins.readFile manifestYAMLFile`.

### Fix

`easykubenix/easykubenix/internal.nix`: changed `options.internal`'s type
from `lib.types.anything` to `lib.types.raw` (single-definition
passthrough, no per-key decomposition -- semantically correct here since
`internal` is only ever defined by this one module, never merged across
multiple modules). Verified:

- Real hetzkube tree, plain `nix eval`, same attrNames call: 2m32.7s w/ 40+
  builds -> 0.6s w/ **zero** builds.
- `internal.manifestJSON`'s actual built content is **byte-identical**
  before/after the fix (9,600,218 bytes, `diff` clean) -- the fix only
  changes when forcing happens, not what gets computed.
- Full nanopynix RPC path (`Session` + `select_attr`, the exact repro
  script below) against the real hetzkube tree: 1.6s total (session open +
  store + eval + the has_attr walk), via a temporary pytest test
  (`tests/test_zzz_scratch_smoke.py`, removed after confirming -- see
  CLAUDE.md guidance on temporary pytest files for this kind of repro).

`easykubenix/easykubenix/kubernetes.nix` also uses `lib.types.anything` for
`generated`/`generatedByPath`/`apiMappings`/etc. -- **not** fixed, and not
believed to need it: those are lists (whose "anything" merge path doesn't
do the same per-key attrset recursion) or attrsets that are only walked
this deeply when a caller actually intends to force them (`force_json`
call sites in `ekn/eval.py` already do this deliberately). Worth a second
look only if a *different* attrname-only walk is ever found stalling on
one of these.

### Separate, since-resolved red herring: worker "Connection lost" on ad hoc scripts

While verifying the fix through the real nanopynix RPC layer, a standalone
`python3 script.py` using `Session(...)` directly crashed immediately with
`WorkerDiedError: Connection lost` on `session.__aenter__()` -- reproduced
both in hetzkube's dev shell and in nanopynix's own. Initially looked like
a regression from the uncommitted `PyLogger` C++ changes. It wasn't: the
exact same `Session` usage passes instantly (0.9s) inside a pytest test
(`@pytest.mark.anyio`) via `direnv exec . pytest ...`. Something about
pytest's harness (asyncio/anyio event loop setup, or multiprocessing
forkserver priming) that a bare `asyncio.run()` script doesn't get. **Moral:
always reproduce nanopynix `Session` issues as a pytest test, not a
standalone script** -- matches this repo's own convention of using
temporary pytest files for exactly this kind of investigation.
