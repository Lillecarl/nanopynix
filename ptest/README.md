# ptest — nanopynix tests 2.0 (prototype)

A greenfield place to design the test suite deliberately, instead of growing it
by accretion. Nothing here runs in CI yet. `pytest.ini` sets
`testpaths = tests`, so a bare `pytest` ignores this directory; run it
explicitly with `pytest ptest`.

## Why

Measured on the existing suite (4-core / 7.7 GB box):

- 1668 tests, 463s wall, **1.2 of 4 cores busy**. No individual test is slow;
  the mean is ~0.28s. It is volume.
- A profiled test spent **13.7s of 14.26s in `epoll.poll`** — the pytest
  process waiting on an RPC worker. The CPU is in the workers, and the
  scheduler has nothing to overlap it with.
- A fresh session + EvalState per test costs **0.140s**; reusing one EvalState
  costs **0.030s**, and *different* expressions cost the same as repeats — the
  nixpkgs base is genuinely amortised in the value graph, not just memoised
  per input.
- A cold store roughly **doubles evaluation time** (`hello`: 0.65s → 0.31s;
  10 packages: 1.62s → 0.88s), because evaluation instantiates and each `.drv`
  write costs ~0.6 ms — which is exactly the ~1670 writes/s SQLite commit
  ceiling measured separately.
- One shared local store root under 1/2/4 concurrent processes: **zero
  errors**. libstore already retries `SQLITE_BUSY` (`local-store.cc`, ~20 call
  sites) behind a one-hour busy timeout, with WAL on by default.

So the two levers are **overlap the waiting** and **stop rebuilding the
evaluator**, and neither needs a second process.

## What is being prototyped

1. **Async concurrency instead of xdist.** The waiting is already async; run
   several test bodies concurrently in one process and the idle cores fill up
   without N sessions, N stores, or N× memory. It doubles as the hardest
   available exercise of our own threading model — worker pool, executor,
   session lifecycle — which is the part most worth battle-testing.
2. **A pooled EvalState with bounded reuse.** Hand out an evaluator a bounded
   number of times, then retire it and start fresh, so the amortisation is
   captured without an evaluator living for all 1668 tests.

## What it has established so far

17 tests, 2.3s, green over repeated runs. Findings, in descending order of how
much they change the design:

- **`inproc` allows exactly one `Session` per process** (`_impl.py`'s
  `_process_guard`). The unit that can multiply is the **evaluator** —
  `Session.eval()` is documented to hand out an independent Nix evaluator with
  its own thread — so that is what the pool pools, and session and store are
  session-scoped singletons. The first draft pooled Sessions and could not
  open a second one.
- **An `EvalSession` owns one Nix thread**
  (`NixThreadExecutor(max_workers=1)`, for `EvalState` affinity). Sharing one
  evaluator between concurrent tests would funnel the whole run onto a single
  thread, so leases are exclusive.
- **Concurrency is real, and it stops paying at two.** Eight 300k-element
  folds, each capacity measured in its own process (see below):

  | capacity | wall | CPU | cores busy |
  | --- | --- | --- | --- |
  | 1 | 0.411s | 0.41s | 0.99 |
  | 2 | 0.225s | 0.43s | 1.90 |
  | 4 | 0.248s | 0.59s | 2.4 |

  The second evaluator is nearly free — 1.83× for 4% more CPU. The third and
  fourth burn 40% more CPU and give back slightly *worse* wall: added
  parallelism exactly cancelled by added contention. `DEFAULT_CAPACITY` is 2
  because of this table, and should be re-measured on other hardware.
- **`process_time`/wall is the honest metric.** Wall clock alone said "4 is
  worse than 2" in one run and "4 is best" in the next. The CPU ratio is stable
  and says what is actually happening.
- **Measurements inside one process are not isolated.** Each parametrization
  gets a fresh pool and fresh evaluators, but the Boehm heap is never reset,
  the store stays warm, and order is fixed. Shared-process numbers drifted 20%;
  one-process-per-capacity numbers repeat to the millisecond and survive
  reordering. Anything comparative must be run that way.
- **Evaluator rotation under concurrent load does not crash.** 24 leases at 2
  apiece across 3 slots — ~12 create/retire cycles overlapping live evaluation,
  each one registering and unregistering a thread with Boehm GC. This is the
  shape of crash this codebase has actually had, and it survives it.
- **On a CPU-bound microbenchmark**, process isolation breaks the two-evaluator
  ceiling and RPC wins past one evaluator. Read this table as a statement about
  Boehm contention, *not* as advice about how to run the suite — the realistic
  workload below reverses the engine ranking. Same 8 evaluations, each configuration measured in its own
  process, both orders:

  | evaluators | engine | layout | wall | CPU | cores |
  | --- | --- | --- | --- | --- | --- |
  | 1 | inproc | — | 0.394–0.438s | 0.39–0.43s | 1.0 |
  | 1 | rpc | 1 worker | 0.500–0.504s | 0.51–0.52s | 1.0 |
  | 2 | inproc | — | 0.228–0.296s | 0.44–0.54s | 1.8–1.9 |
  | 2 | rpc | 1 worker × 2 | 0.370–0.449s | 0.68–0.82s | 1.8 |
  | 2 | rpc | 2 workers × 1 | 0.274–0.301s | 0.52–0.55s | 1.8–1.9 |
  | 4 | inproc | — | 0.344–0.352s | 0.73–0.75s | 2.1 |
  | 4 | rpc | 2 workers × 2 | 0.198–0.207s | 0.59–0.63s | 3.0 |
  | 4 | rpc | 4 workers × 1 | 0.163–0.190s | 0.51–0.55s | 2.9–3.1 |

  At four evaluators RPC is **2× faster in wall clock while using 30% less
  CPU** than inproc. The two 2-evaluator RPC rows are the cleanest evidence:
  identical work and identical evaluator count, but splitting them across two
  worker processes instead of two threads in one costs ~30% less CPU. The
  overhead the extra threads pay is per-process-shared-state, which is what the
  Boehm hypothesis predicts.
- **RPC overhead is real, small, and repaid immediately**: at one evaluator RPC
  costs ~0.08s wall and ~0.10s CPU over 8 evaluations against inproc, i.e.
  **~10–12 ms per evaluation** of wire tax. inproc wins at one evaluator; RPC
  wins at every count above one.
- **Worker startup is 0.82s for the first and ~0.2s for each after** (1.36s for
  four). One-time against a 463s suite, but it means workers must be pooled for
  the session, never spawned per test.
- **`rpc.Session`'s "one thread-confined EvalState at a time" docstring is
  stale.** Three concurrent evaluators in one worker return three correct
  answers; the worker allocates a fresh executor and handle per `open_eval`
  (`_worker_eval.py:233`). Worth fixing in the docstring — it understates what
  the engine can do, and the `(1, 2)` row above depends on it.
- **The nixpkgs amortisation reproduces as a test**: first attribute 0.12s,
  each subsequent *different* attribute 0.021–0.029s.
- **One store serves 16 concurrent writers in-process** with no `SQLiteBusy`
  escaping, matching the earlier multi-process measurement.

## The realistic workload overturns the microbenchmarks

Everything above this section was measured with `builtins.foldl'` over a
generated list: pure CPU, zero store I/O. That was the wrong instrument, and it
produced two conclusions that a realistic workload reverses. The suite it is
meant to model runs at **1.2 of 4 cores**, with a profiled test spending 13.7s
of 14.26s in `epoll.poll` — measuring a CPU-bound expression and concluding
things about a wait-bound suite was a methodology error, not a close call.

`test_nixpkgs_workload.py` does the real thing instead: force `drvPath` on a
pre-selected sample of nixpkgs packages, from a **fresh store root per
configuration**, with wall clock as the metric and CPU only as explanation.
60 packages, cold store:

| engine | store | workers | wall | CPU | cores |
| --- | --- | --- | --- | --- | --- |
| inproc | local | 1 | 10.46s | 9.80s | 0.94 |
| inproc | local | 2 | 9.56s | 14.12s | 1.48 |
| inproc | local | 4 | **8.86s** | 19.06s | 2.15 |
| rpc | local | 1 | 10.32s | 9.62s | 0.93 |
| rpc | local | 2 | 10.62s | 13.09s | 1.23 |
| rpc | local | 4 | 12.48s | 19.39s | 1.55 |
| rpc | daemon | 1 | 15.50s | 14.61s | 0.94 |
| rpc | daemon | 2 | 15.02s | 18.91s | 1.26 |
| rpc | daemon | 4 | 17.28s | 27.25s | 1.58 |

What flips:

- **Parallelism barely pays.** inproc 1→4 workers buys 1.18× for 2× the CPU.
  RPC gets *worse* with width (10.32s → 12.48s), as does rpc+daemon.
- **inproc beats RPC at every width**, the opposite of the microbenchmark,
  where RPC won at every count above one. Per-`.drv` round trips cost more than
  process isolation saves.
- The likely cause of both: each evaluator independently imports nixpkgs
  (~9–10s of that 10.46s), so sharding 60 packages four ways **multiplies a
  fixed cost instead of dividing work**. This is in direct tension with the
  amortisation finding — sharing an evaluator amortises the base but
  serialises; N evaluators parallelise but each re-pays it. Whether the slope
  improves once per-package work dominates is what the 500-package run answers.

### A cell was missing, and it made the daemon claim unreadable

The first version of this matrix omitted inproc+daemon, on the reasoning that
inproc against a daemon URI is "the same client code against a different URI".
That was wrong. Without it, "rpc+daemon is 50% slower than rpc+local" confounds
**the daemon protocol** with **the RPC wire**, and there is no way to attribute
the cost to either. Any daemon-versus-local conclusion drawn from the table
above should be treated as provisional until the full 2×2 is in.

## The isolation contract

The existing suite has three fixture tiers (`shared_nix_environment`,
`l1_nix_environment`, `isolated_nix_environment`) that grew from what was
available rather than from a stated rule. Here the rule comes first — a test
declares what must be true at its start, and gets the cheapest thing that
satisfies it:

| Need | Gets | Cost |
| --- | --- | --- |
| Evaluate expressions | a pooled EvalState, possibly used before | ~0.03s |
| A private evaluator (mutates files, asserts on eval cache) | a fresh EvalState, retired after | ~0.14s |
| Read/write store paths | the shared warm store | free |
| Assert on store *contents* (GC, validity counts) | a private store | expensive, rare |

Anything not on this table is a gap in the contract, not a reason to add a
fourth tier.

## Open questions this directory exists to answer

- **Boehm is now the leading suspect, not merely the first one** — the RPC
  comparison above shows the penalty tracks *processes shared*, not threads
  used. Not yet proven: it could be any process-global state in libstore or the
  bindings. Confirming it properly means a Boehm-level measurement
  (`GC_get_full_gc_total_time`, or the collector's own stats), which nothing
  currently binds. The GIL is ruled out — released in `eval_string`
  (`nix_expr.cpp:462`).
- **Is the answer a hybrid?** RPC wins on throughput past one evaluator; inproc
  wins on latency at one and needs no wire. A suite could run N workers × 2
  evaluators and get both, at the cost of running most tests against the engine
  they are not primarily testing.
- What is the right reuse bound before an evaluator must be retired — measured
  against memory, not guessed. `DEFAULT_MAX_LEASES = 25` is still a guess.
- **There is no Boehm `GC_gcollect` binding.** `collect_garbage` is *store* GC.
  Retiring an evaluator can only drop references and let Boehm collect under
  allocation pressure — which is also why measurements in one process cannot be
  isolated from each other. An explicit collect was tried once and took the
  process down with a signal, but that predates the thread-registration work in
  `EvalSession.open`/`close`, so it is worth retrying rather than assuming.
- Does a shared evaluator survive tests that mutate files? `EvalSession`
  already has `reset_file_cache()`, which means someone hit this before.
- Retiring on any exception costs one evaluator per failing test. Cheap when
  the suite is green, and the alternative is a bad evaluator contaminating the
  next test — but it is a guess about which failures dirty an evaluator.
