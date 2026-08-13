# The collector, threads, and what the investigation proved

nanopynix runs several `EvalState` instances at the same time, each on its own
thread, in one long-lived process. Nix itself runs one `EvalState`, on one
thread, and then the process ends. This file records what that difference
costs, what the investigation proved, and what it excluded.

**Read this before you open a new hypothesis about the collector.** Each
section gives the claim, the measurement that tested the claim, and the
verdict. A hypothesis that this file already excludes needs new evidence, and
not a new argument.

Issues #53, #69, #70 and #72 are the subjects. The issue holds the status, and
this file holds the evidence.

## What upstream Nix gives us, and what it does not

libexpr enables `GC_THREADS`, `SymbolTable` uses a concurrent set, and
`counter.hh` says its design prevents contention "when multi-threaded
evaluation is enabled". So concurrent evaluation is a direction that upstream
prepares for.

Upstream does not test it. The two unit tests that build more than one
`EvalState` (`nix_api_expr.cc`, `nix_api_external.cc`) build them one after
the other, on one thread. `eval.hh` states no threading contract, except one
line that says a return value is not thread safe.

Three pieces of process-global mutable state reach every `EvalState`:

- `globalConfig` (`config-global.cc`), and the `FIXME: don't use a global
  variable` on `experimentalFeatureSettings` (`configuration.hh`).
- `static bool gcInitialised` in `eval-gc.cc`, which **no mutex guards**. Two
  sessions that open at the same time, on two threads, can both enter
  `initGC`.
- `static Counter nrThunks` in `eval.cc`, shared by every `EvalState`.

**We are the first consumer to push on this.** Treat a failure here as ours to
prove, and not as a Nix defect, until the evidence names Nix.

## Proven

### Boehm's first thread must outlive every collection

bdwgc has one statically allocated `GC_thread`, `first_thread`. `GC_thr_init`
gives it to whichever thread reaches `GC_INIT()`, and marks it
`DETACHED | MAIN_THREAD`. `GC_delete_thread` unlinks `first_thread` but never
frees it, and no thread exit clears the flags. `GC_suspend_all` signals every
entry that is neither the caller nor `FINISHED`.

`Session.open` reached `GC_INIT()` on a `nix-store` executor thread, which
exits with the session. A core dump shows the result directly: `GC_threads`
held `first_thread` with an id that `info threads` does not list.

| `GC_INIT()` runs on | crashes |
|---|---|
| a `nix-store` pool thread | 3 in 4 |
| a thread that never exits | 0 in 6 |

Issues #53, #69 and #72 are that one condition, with three outcomes: a fault
inside `pthread_kill`, an `EINVAL` return, and the 150-retry
`resend_lost_signals` abort.

### Where the collector starts, and why

`init_libexpr` starts the collector on a thread that it creates, names
`nix-gc-owner`, and never lets exit. `std::call_once` makes it happen once.

**The owner lives in C++, and not in Python.** A rule that one Python function
obeys is a rule that every other entry point breaks. The test fixtures of this
repository build an `EvalState` directly, with no `Session`, and an owner that
`NixCore.initialize` created never existed for them. Measured: the Python-side
owner passed the end-to-end test and failed the binding tests.

**The start is lazy, and must stay lazy.** bdwgc installs no atfork handlers
unless something calls `GC_set_handle_fork` before `GC_INIT`, and
`GC_handle_fork` is `FALSE` by default (`misc.c:196`). Nix never calls it. So a
forkserver parent that brings the collector up hands every worker child a
thread table that nothing fixes up. A process that imports nanopynix and opens
no session must get no collector.

`std::call_once` also covers a gap upstream: the `gcInitialised` flag that
`nix::initGC` tests is a plain `static bool` with no mutex, so two sessions
that open at the same time can otherwise both enter it.

**`GC_set_handle_fork(1)` is deliberately not called.** It installs atfork
handlers that take the allocation lock across every `fork`, and libstore forks
for every build. That is its own decision, with its own risk, and lazy start
removes the reason to take it now.

### Move all of `nix::initGC`, or none of it

Boehm consults `GC_valid_offsets` in the mark path
(`GC_push_contents_hdr`, with `do_offset_check` true). An unregistered
displacement makes Boehm treat a tagged pointer as a false reference. It
black-lists the pointer instead of marking the object, so a live value dies.

Nix packs a 3-bit discriminator into the pointer
(`discriminatorBits = 3`, `value.hh`), and registers displacements 1 to 7. The
two match, and there is no off-by-one.

An attempt that moved only Boehm's own bring-up left `initGC` to call
`GC_set_all_interior_pointers(0)` with `GC_is_initialized` already true. That
call resets the valid offsets and runs `GC_bl_init_no_interiors()` over a heap
in use. Four evaluators then read a value as the wrong type, 6 runs in 6,
where the unmodified build fails 0 in 6.

### The collector is in the causal path of #70

The crash rate follows the collection rate, on the soak, with the stop-the-world
patch in place:

| Boehm setting | runs | SIGSEGV |
|---|---|---|
| `GC_DONT_GC=1` | 20 | 0 |
| default | 30 | 1 |
| `GC_FREE_SPACE_DIVISOR=64` | 15 | 5 |

### Two evaluators are enough for #70

The full suite died once in `nanopynix/tests/rpc/test_log_backpressure.py`,
with two `nix-eval_0` threads live, in `nix::ExprVar::eval`. The earlier note
said "the four-evaluator test", and that is too narrow.

### The thread that builds an evaluator must be registered with Boehm

`GC_push_all_stacks` (`pthread_stop_world.c:778`) walks Boehm's thread table,
pushes the stack of each entry as a root, and visits nothing else. Line 918
aborts with `Collecting from unknown thread`, and it does so in one case only:

```c
if (!found_me && !GC_in_thread_creation)
    ABORT("Collecting from unknown thread");
```

`found_me` names the **calling** thread. A thread that is absent and is not
the caller gives no abort at all. So an unregistered thread that drives an
evaluator has two outcomes, and the thread that starts the collection picks
between them:

| the collection starts on | outcome |
|---|---|
| the unregistered thread | `ABORT`, and the process dies |
| another registered thread | no abort, and this stack is never scanned |

The second outcome is silent. The values that only that stack refers to are
unreachable, the collector frees them, and the next read of one of those
pointers gives whatever took the memory.

**`tests/conftest.py` builds its evaluator with `nanopynix.EvalState(store)`,
on the pytest main thread.** Only `NixThreadExecutor` called
`enter_evaluator_thread`, which was the one caller of `GC_register_my_thread`,
so the main thread evaluated Nix while absent from the table.

The evidence is the amplified arm of CI run 30966905346. The abort message is
at `.rodata` 0x29ca0 of the `libgc.so.1` of that job, and the frames resolve
to `GC_malloc_kind_global` → `GC_generic_malloc_inner` → `GC_collect_or_expand`
→ `GC_try_to_collect_inner` → `GC_stopped_mark` → `GC_mark_some` → `abort`.

`PyEvalState::init` now calls `nanopynix_ensure_gc_thread_registered`, so a
caller that never touches the executor is covered.

**The differential is one line, and it needs no statistics.** Comment that one
call out, rebuild, and build an evaluator on the main thread:

| that one line | `_gc_thread_is_registered()` | `_gc_collect()` from the main thread |
|---|---|---|
| absent | `False` | refused: "a thread that Boehm GC does not know" |
| present | `True` | succeeds |

Two runs decide it. Prefer this over a rate: the soak below could not
reproduce the failure often enough to measure either side.

## Excluded

Each of these has evidence, and needs new evidence to reopen.

- **A leaked registration of ours.** 17 enters and 17 exits over a crashing
  run, per thread id, with zero `GC_DUPLICATE`.
- **A stale entry from a recycled `pthread_t`.** The same evidence, and
  `enter_evaluator_thread` logs that case.
- **The base environment overflow (#52).** The crash is measured with
  `nix-base-env-size.patch` in place.
- **The stop-the-world abort (#72).** A different signal, and it is gone: 0
  aborts in 30 runs.
- **A foreign thread using an evaluator.** `PyEvalState::checkThread` gates
  every accessor. **This entry was too strong until the registration above.**
  `checkThread` answers which thread may drive an evaluator, and it never
  asked whether the collector knows that one thread.
- **A data race that ThreadSanitizer can see.** Every TSan job passes with the
  soak in it.
- **Multiple evaluators plus frequent collections, on their own.** The two
  multi-evaluator modules with `GC_FREE_SPACE_DIVISOR=64`: 0 failures in 10
  runs. Something else in the suite is a necessary ingredient.
- **A teardown that is mistimed against the registration.** The report of #70
  puts a concurrent `EvalSession.close` first, and the ordering it suspects is
  not there. Two facts, and both are in the code rather than in a rate:
  1. The main thread in that report is not running `~EvalState`.
     `_nix_executor.py:430` is `self._pool.submit(finalizer).result()`, so
     that thread is blocked and the finalizer runs on the **closing
     evaluator's own thread**. The operation that races the two live
     evaluators is `GC_unregister_my_thread`, on the thread that owns the
     registration.
  2. The evaluator is torn down while its thread is still a root.
     `EvalSession.close` (`inproc/_impl.py:1223`) awaits `local.close`
     through `_run_closing`, which goes to the evaluator's own one-worker
     pool, and only then runs `self._executor.shutdown(wait=True)` in its
     `finally`. `_objects.py:440` gives the reason the handle drops there:
     otherwise `nix::EvalState` outlives `executor.shutdown` and its AST
     arena and symbol table are destroyed on whichever thread drops the
     handle last.

  So no window exists in which an evaluator's `EvalState` is destroyed, or
  its values freed, after its thread stopped being a GC root. The one path
  that skips step 1 is `EvaluatorAbandonedError`, and the report is not that
  path: the close completed, so the thread was answering. This excludes the
  ordering, and **not** the teardown: `GC_unregister_my_thread` itself, and
  the collection a teardown makes likely, are both still open.

## Not yet explained

**#70.** A `Value` reads as the wrong type during evaluation. The shape fits a
live object that the collector reclaims and then hands out again.

The reproduction used to be the **whole** test suite, at about 1 failure in 5
runs of 8 minutes, measured locally. A narrower selection does not reproduce,
even with collections forced.

**That rate is stale, and CI says so.** The collector-owner change (7f6ef4e7)
sits between that measurement and today. On code that carries it and still
lacks the thread registration, the whole suite ran 30 times in CI:

| run | ref | full-suite runs | #70 failures |
|---|---|---|---|
| 30966905346 | `9b28f36d` | 15 | 0 |
| 30969557667 | `develop` | 15 | 0 |

The one failure in run 30966905346 was the unknown-thread abort above, and
not this issue. So #70 has not reproduced once since the collector gained an
owner thread, and no arm of any size can now measure whether the registration
helped. **Reopening #70 needs a reproduction first, not another soak.**

The registration (`99f74d82`) landed after that measurement, and the streak
continues on code that carries it. Run 31050794050 is a full matrix on
`develop` at `7ec0114a`, and each of its four UBSAN jobs passed:
`test-ubsan-nix_2_31`, `test-ubsan-nix_2_34`, `test-ubsan-nix_2_35` and
`test-ubsan-git`. UBSAN is the job that caught #70 the one time anything did,
so a green one is the only routine signal this issue has. It is a weak signal
at a rate of 5 percent, and it is recorded here to keep the streak countable
rather than to argue that the registration fixed anything.

**The unregistered main thread is a mechanism that fits every measurement of
#70, and it is not yet proven to be the cause.** The shape agrees: an
unscanned stack loses exactly the values that only it refers to, and the rate
follows the collection rate.

**The amplified arm cannot decide it, and this is measured.** The whole
amplified evidence, on code that has the defect:

| run | ref | amplified runs | failures |
|---|---|---|---|
| 30966905346 | `9b28f36d` | 5 | 1 |
| 30969557667 | `develop` | 15 | 0 |

One failure in 20 is about 5 percent, and 15 runs of a 5 percent event expect
0.75 failures. So an arm of 15 that comes back clean says nothing about
whether the registration helped. **Do not report a clean amplified arm as
evidence for a fix.** Find an amplifier that reproduces, or use the one-line
differential above, which decides in two runs.

## The instruments, and what each one cannot see

**Do not read a green sanitizer job as evidence against a collector bug.**

| job | keeps the collector | detects | blind to |
|---|---|---|---|
| `test-asan-*` | **no** (`requiresNoGC = isAddress`) | bad heap access | every Boehm defect |
| `test-tsan-*` | yes, instrumented | data races | reachability errors |
| `test-ubsan-*` | yes | undefined operations | reachability errors |
| `test-nogc-*` | no | ordinary defects | every Boehm defect |

libexpr refuses ASan together with the collector, and `nix/sanitizer.nix` gives
the reason: the collector can free an object that is still live, and ASan then
reports a read of memory that Boehm freed. The report is not evidence.

A collection that reclaims a live object is a reachability error. The mark
phase stops the world and holds the allocation lock, so there is no race for
TSan to report, and no invalid access at the moment the object dies.

**gdb suppresses the bug.** 8 clean runs under gdb, against 4 in 4 and 6 in 6
without it. The ptrace stops at thread creation and thread exit change the
timing. Use a post-mortem core through `coredumpctl`.

## How to reproduce, today

The selection of #53, before the fix, crashed 6 times in 6, in 48 seconds:

    pytest nanopynix/tests/test_logging.py nanopynix/tests/test_verbosity.py \
        nanopynix/tests/inproc nanopynix/tests/bindings

#70 needs the whole suite:

    pytest

Raise `GC_FREE_SPACE_DIVISOR` to make the collector run more often. Set
`GC_DONT_GC=1` to take the collector out of the picture, which is the control
arm for every collector hypothesis.

`NANOPYNIX_GC_THREAD_DEBUG=1` logs every registration, and
`NANOPYNIX_GC_THREAD_DEBUG_FILE` sends the log to a path. Use the file:
pytest captures at the file-descriptor level, and a crash loses the buffer.
