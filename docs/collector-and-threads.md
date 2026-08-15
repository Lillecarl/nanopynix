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

**Measured again, on today's code, at 2.34.8 and on one machine.** The arm
below is the soak alone, and the amplified setting is a pair:

| arm | heap settings | collector | runs | failures |
|---|---|---|---|---|
| default | default | on | 12 | 0 |
| amplified | 8 MiB, divisor 64 | on | 12 | 3 |
| amplified, inproc alone | 8 MiB, divisor 64 | on | 8 | 4 |
| control | 8 MiB, divisor 64 | **off** | 8 | 0 |

**The control is the row that decides it.** It holds each heap setting of the
amplified arm and adds `GC_DONT_GC=1`, so the process takes the same memory
pressure and the same heap layout, and collects nothing. Every failure goes
away. Memory pressure is therefore not the cause, and the heap layout is not
the cause. Collection is.

Two of the three failures of the amplified arm are SIGSEGV. The third is #70's own symptom, and it is the
first sighting of that symptom since the collector gained an owner thread:

```
… while evaluating list element at index 265468
error: cannot convert a function to JSON
at «string»:1:19:
     1| builtins.genList (x: x) 12000000
```

`genList (x: x)` gives a list of integers, and Nix read element 265468 as a
function. That is a live object which the collector reclaimed and handed out
again, which is what this issue says.

**Amplification needs both settings.** `GC_FREE_SPACE_DIVISOR` alone changes
nothing while the heap has slack, and `eval-gc.cc:88` gives it 384 MiB of
slack unless `GC_INITIAL_HEAP_SIZE` is set. Measured with `_gc_stats` on an
evaluator thread, over the same workload:

| setting | collections |
|---|---|
| default | 6 |
| `GC_FREE_SPACE_DIVISOR=64` | 6 |
| `GC_INITIAL_HEAP_SIZE=8388608` | 63 |
| both | 129 |

The soak is heavier than that workload and exhausts the 384 MiB heap, so the
divisor does act there; the table above is why the pair is the arm to use.
Note that `ci/steps.nix` raises `GC_INITIAL_HEAP_SIZE` to 2 GiB for the TSan
soak, which moves the other way on purpose, and says so.

### The narrow reproduction

The inproc soak alone, under the amplified pair, fails **4 runs in 8**, at
about 10 seconds for each run:

    GC_INITIAL_HEAP_SIZE=8388608 GC_FREE_SPACE_DIVISOR=64 \
        pytest -m soak -k inproc --soak-seed=0 --nix-test-backends local

That replaces "the whole suite, about 1 failure in 5 runs of 8 minutes" as the
reproduction of #70. Every arm that was too expensive against the old one is
affordable against this one.

**Select the soak with `-m soak`, and name no path.** A path argument moves
pytest's rootdir, and `discover_roster` reads `nanopynix/tests` under the root
it is given. The roster then comes back empty. That state used to skip, and it
now fails.

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
- **A live pointer at a displacement that Boehm does not accept.**
  `GC_push_contents_hdr` black-lists a reference at an unregistered offset
  rather than marking the object it names, and Nix packs a 3-bit discriminator
  into each `Value` pointer. `NANOPYNIX_GC_ALL_INTERIOR_POINTERS=1` marks every
  offset valid, and the failure rate does not move:

  | arm | runs | failures |
  |---|---|---|
  | amplified | 12 | 3 |
  | amplified, every offset valid | 12 | 4 |

  The differential is in `nix_expr.cpp`, and it states this criterion itself.
  It stays until #70 closes, because a later change to how Nix packs that
  pointer puts the class back on the table.
- **A thread that the collector does not know.** Registration is balanced in a
  run that fails with the wrong-type read: 57 registrations and 57
  unregistrations, which is what a clean run gives.
  `NANOPYNIX_GC_THREAD_DEBUG=1` on the short reproduction. The count is now per
  process, and one process of the four registers every evaluator thread. A run
  that ends in SIGSEGV instead gives an unbalanced count, because the process
  dies with its threads live, and that says nothing.
  **This excludes the accounting, and not the scan.** A thread that Boehm
  knows and does not stop is a different failure, and #72 is the precedent for
  one.
- **A thread that stops too slowly for stop-the-world.** `GC_stop_world`
  resends its signal to a thread that does not acknowledge in time, and it
  logs `Resent %d signals after timeout`. It warns `Lost some threads while
  stopping or starting world?!` when the count still does not balance. Both
  strings are in this build of the library. `GC_PRINT_STATS=1` with
  `GC_LOG_FILE` on eight runs of the short reproduction, of which runs 2 and 5
  failed: **zero occurrences of either line, in all eight**. Only the banner
  `Will retry suspend and restart signals if necessary` appears.

- **The tolerated branch of our own patch.**
  `nix/patches/boehmgc-tolerate-suspend-thread-exit-race.patch` widens
  `GC_suspend_all` and `GC_start_world` to accept `EINVAL` beside `ESRCH`. That
  branch runs `n_live_threads--; break;`, so it leaves a thread unsuspended and
  leaves its entry in `GC_threads`, and the count then balances by
  construction. It fitted every measurement this issue had.

  The patch now logs from both branches, through `GC_COND_LOG_PRINTF`, so
  `GC_PRINT_STATS=1` reports the skip. Twelve runs of the short reproduction,
  three of which died with SIGSEGV: **zero skips at suspend and zero at resume,
  in all twelve**. The control is exact, because the banner `Will retry suspend and
  restart signals if necessary` comes from the same macro in the same file, and
  it appears in every log.

  **So the branch is never taken here, and #70 is not #72 under another name.**
  Keep the log lines. They cost nothing in a normal run, and they turn the one
  silent branch in this file into an observable one.
- **Parallel marking.** The stats log says `Started 3 mark helper threads`, so
  the mark phase runs on four threads. `GC_MARKERS=1` removes the helpers, and
  the log then carries no such line.

  | arm | runs | SIGSEGV | other |
  |---|---|---|---|
  | amplified | 12 | 3 | 0 |
  | amplified, one marker | 12 | **4** | 2 |

  The rate does not fall. **Count the SIGSEGV column, and not the runs that
  merely failed.** The soak also fails with a `TimeoutError` from its own
  deadline, which is a slow machine and not this issue, and one arm of this
  investigation reported 6 against 3 by adding the two together.
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
not this issue. So #70 did not reproduce once between the collector-owner
change and 2026-08-14, and no arm of any size could measure whether the
registration helped.

**That paragraph asked for a reproduction before another soak, and the
amplified soak above is it.** The arm to run is the pair of Boehm settings,
and not the divisor alone. Read "The collector is in the causal path of #70"
for the rates and for the command.

**The streak ended, and the soak is the new reproduction.** Run 31820106000
is a full matrix on `ci-develop` at `0349648a`. Its `test-local-nix_2_35` job
died with SIGSEGV in the **soak**, 0.6 seconds in, with three evaluator
threads live and the same frame as before:

```
Thread 0x00007f9d3c3fe6c0 [nix-eval_0]:  _objects.py:534 in eval_string
Current thread 0x00007f9d3ffff6c0 [nix-eval_0]:  _objects.py:543 in repl_eval_string
Thread 0x00007f9d387fd6c0 [nix-eval_0]:  _objects.py:534 in eval_string

libnixexpr.so.2.35.1, at nix::ExprVar::eval(nix::EvalState&, nix::Env&, nix::Value&)+0x120
```

Four lanes were in flight, and one of them
(`test_a_cancelled_interruptible_operation_frees_the_evaluator`) closes an
evaluator, which is the condition this issue is named for. The same job on
the same commit then passed three times out of three, so the rate is about 1
in 4 on this job.

**This matters because the soak is 0.6 seconds where the whole suite was 8
minutes.** An arm that forces collection was not affordable against the old
reproduction. Against this one it is. Issue #70 carries the lane composition
and the run links.

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

### Stop-the-world reaches every thread

Four measurements say the same thing. All are on the short reproduction, and
each covers a run that failed:

| question | instrument | answer |
|---|---|---|
| does Boehm have an entry for every thread? | `NANOPYNIX_GC_THREAD_DEBUG=1` | yes, 57 and 57 |
| does any thread fail to acknowledge in time? | `Resent %d signals after timeout` | never, 0 of 12 |
| does the count of live threads ever drop? | `Lost some threads...` | never, 0 of 12 |
| does a thread get skipped at suspend or resume? | the log added to our patch | never, 0 of 12 |

`GC_stop_world` waits until the acknowledgement count equals the count of live
threads, and it logs only when that wait passes 100 ms. Zero `Resent` lines
therefore mean that **every registered thread acknowledged the suspend signal,
on every one of the 117 collections of a failing run.** Parallel marking is
excluded separately, above.

**This closes the reaching of a thread, and not the scan of its stack.**
`GC_push_all_stacks` runs after the world stops, and it can still miss a stack
in two ways that none of the four measurements covers:

1. **The `FINISHED` flag.** The loop runs `if (p -> flags & FINISHED)
   continue;`, and it does not count such an entry. `GC_unregister_my_thread`
   sets that flag for a joinable thread. A thread that unregisters and keeps
   running therefore keeps its stack out of every later collection.
2. **The recorded bounds.** For a thread that is not the main one, the loop
   takes `hi = p -> stack_end`, which `GC_register_my_thread` recorded from
   `GC_get_stack_base` at registration. It takes `lo` from the stack pointer
   saved at suspend. A wrong `hi` leaves the top of the stack unscanned, and
   nothing reports it.

`GC_PRINT_VERBOSE_STATS=1` prints `Pushed %d thread stacks` for each
collection, which measures the first of the two. **That arm cannot run against
this reproduction as it stands.** Four processes initialise the collector, they
all write to the one path that `GC_LOG_FILE` names, and no line carries a pid.
The sequence of counts is therefore interleaved and no count belongs to a known
process. `NANOPYNIX_GC_THREAD_DEBUG=1` does carry a pid now, and it says that
**one** of those four processes registers an evaluator thread. Give the
collector log the same attribution, or reduce the reproduction to one process,
and the arm becomes readable.

**The other family is a root that is not a thread stack.** A `Value` reachable
only through memory the collector does not trace is invisible to a correct scan
of every stack. One candidate is already excluded: `PyValue` holds a
`nix::RootValue`, which `nix::allocRootValue` builds with
`traceable_allocator`, and bdwgc's `traceable_allocator::allocate` calls
`GC_MALLOC_UNCOLLECTABLE`. That block is traced and never collected, so a
`PyValue` in Python's own heap still keeps its `Value` alive.

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

#70 fails 4 runs in 8, at about 10 seconds for each run:

    GC_INITIAL_HEAP_SIZE=8388608 GC_FREE_SPACE_DIVISOR=64 \
        pytest -m soak -k inproc --soak-seed=0 --nix-test-backends local

The whole suite reproduced #70 before this, at about 1 failure in 5 runs of 8
minutes. Use the command above instead.

**Both settings are necessary.** `GC_FREE_SPACE_DIVISOR` raises the collection
rate only when the heap has no slack, and `GC_INITIAL_HEAP_SIZE` is what takes
the slack away. The measurement is in "The collector is in the causal path of
#70". Add `GC_DONT_GC=1` to the same command to take the collector out of the
picture, and keep each heap setting: that is the control arm, and it fails 0
runs in 8 where the arm above fails 4.

**Select the soak with `-m soak`, and name no path.** A path argument moves
pytest's rootdir, and the roster is read relative to it.

`NANOPYNIX_GC_THREAD_DEBUG=1` logs every registration, and
`NANOPYNIX_GC_THREAD_DEBUG_FILE` sends the log to a path. Use the file:
pytest captures at the file-descriptor level, and a crash loses the buffer.
