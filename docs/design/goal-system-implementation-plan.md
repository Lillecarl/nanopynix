# Goal System Implementation Plan

## Purpose

This plan tracks the reintroduction of a pynixd-owned goal system after the
daemon-delegation experiment ran into derivation/build locking tradeoffs. The
goal system should make `BuildPaths`, `BuildPathsWithResults`, and
`QueryMissing` share dependency walking while keeping side effects explicit and
easy to audit.

The current direction is:

- request-local goal runs, not a global cache of coordinator goals;
- singleton scheduler-owned work lanes for deduplicated side effects;
- build dedup in the build queue by `.drv` path for `BuildMode.NORMAL`;
- substitution import dedup by output path;
- substitution availability cached by path and store with positive and negative
  TTLs;
- distinct read-only planning goals and mutating build goals, with shared helper
  code where it stays understandable.

## How To Use This Plan

Every task has these fields:

- `Status`: `todo`, `in-progress`, `blocked`, `done`, or `deferred`.
- `Owner`: `primary`, `subagent-ok`, or a named owner.
- `Can delegate`: whether a subagent may do the task.
- `Depends on`: task IDs that must be complete first.
- `Validation`: focused checks for the task.
- `Commit`: whether this task should be committed alone or batched.
- `Notes`: decisions, risks, and follow-up details.

Subagents may edit files and run focused validation, but must not use `jj` or
other VCS tools. The primary agent owns all status checks, diffs, commits, and
squashes.

## Validation And Commit Rules

- Do not run the full test suite for every small task.
- During architecture-heavy implementation, prioritize `pyright` and focused
  behavior tests. Defer pure lint/format cleanup until later unless it blocks
  type checking, execution, or understanding.
- For delegated or mechanical changes, run focused tests or `direnv exec . just
  cheap` at most.
- Run `direnv exec . just precommit` only near the end of a coherent slice,
  before committing.
- Use `jj --no-pager status` and `jj --no-pager diff` before every commit.
- Prefer one commit per coherent architecture slice, not one commit per tiny
  helper edit.
- Commit style: `jj commit -m "goals: <short imperative summary>"`.
- If a change is a direct fixup to the previous goal-system commit, prefer
  `jj squash --use-destination-message` after review.
- Never include unrelated working-copy changes in a commit. At the time this
  plan was written, `REASONIX.md` was already an unrelated added symlink.

## Architecture Decisions

### Request-Local Goal Runs

`BuildPaths`, `BuildPathsWithResults`, and `QueryMissing` should each create an
isolated request-local goal run. A run owns traversal state, root results,
continue-on-error behavior, fail-fast behavior, and read-only versus mutating
policy.

Completed coordinator goals should not be cached globally. A later invocation
should discover current truth from the local store, substitution availability
cache, and scheduler queues.

### Scheduler-Owned Work Lanes

The scheduler may be a singleton "work scheduler", but it should keep typed
lanes:

- build lane: existing build queue, builder assignment, build subscribers, and
  active build dedup by `.drv` path;
- substitution lane: availability cache, per-store query health logs,
  background probe tasks, active import dedup by path, and import concurrency
  limits.

This keeps global dedup at the side-effect boundary instead of globalizing the
entire goal graph.

### Read-Only Versus Mutating Goals

Use separate read-only and mutating entrypoints:

- `QueryMissing` planning goals may inspect local validity and substitution
  availability, but must not import, build, register realisations, or mutate
  local state.
- `BuildPaths` and `BuildPathsWithResults` goals may substitute and build.

Prefer separate root/goal classes for read-only and mutating paths. Share helper
functions or a small base class only where the shared code stays obvious.

### Build Mode Scope

Only `BuildMode.NORMAL` is in scope for the first goal-system implementation.
`BuildMode.CHECK` and `BuildMode.REPAIR` should return clear client-visible
errors instead of silently behaving like normal builds.

### Substitution Semantics

Substitution remains path-granular from the goal system's perspective.

The substitution lane should expose:

- `can_substitute(path)`: query path info through Store APIs, return quickly on
  the first positive response from stores worth waiting for, and update caches;
- `get_substituter(path)`: check cache and block on healthy substituters to pick
  the highest-priority positive source;
- `substitute(path)`: deduplicate active imports by path and import from the
  source selected by `get_substituter`.

`can_substitute` is for graph traversal and GUI-ish `QueryMissing` sizes.
`substitute` must use path info from the selected substituter because daemon
store streaming needs the selected source's `nar_size`.

## Task Ordering

### T00 - Confirm Current Goal-System Surface

- `Status`: done
- `Owner`: primary
- `Can delegate`: no
- `Depends on`: none
- `Validation`: read current `pynixd/goals`, `pynixd/scheduler.py`,
  `pynixd/build_queue.py`, `pynixd/proxy.py`, and current tests touching goal
  behavior.
- `Commit`: no commit; review task only.
- `Notes`: Re-synced against the live tree. The current tree contains a global
  `GoalEngine` prototype; the first implementation slice reshapes construction
  toward request-local use instead of blindly extending the global cache.

### T01 - Define Request-Local GoalRun Skeleton

- `Status`: done
- `Owner`: primary
- `Can delegate`: no
- `Depends on`: T00
- `Validation`: unit import/type checks for new modules; no functional tests yet.
- `Commit`: batch with T02 or T03 unless large.
- `Notes`: Initial slice creates a fresh `GoalEngine` per daemon build-path
  request and removes it from `PynixdContext`, so completed coordinator goals are
  no longer cached process-wide. A later slice may rename this to `GoalRun` or
  split services from run state.

### T02 - Add Explicit BuildMode Gate

- `Status`: done
- `Owner`: subagent-ok
- `Can delegate`: yes
- `Depends on`: T00
- `Validation`: focused unit or functional test proving `CHECK` and `REPAIR`
  return clear errors while `NORMAL` still enters the goal path.
- `Commit`: can batch with T01.
- `Notes`: Non-`NORMAL` modes now raise a clear client-visible error at the goal
  entrypoint. This intentionally avoids broader build-mode semantics.

### T03 - Split Read-Only Planning From Mutating Ensure

- `Status`: done
- `Owner`: primary
- `Can delegate`: no
- `Depends on`: T01
- `Validation`: focused tests for one opaque path, one known-output derivation,
  and one missing path in both planning and mutating modes.
- `Commit`: commit as its own architecture slice.
- `Notes`: A first read-only `QueryMissingPlanGoal` entrypoint exists and
  preserves the old limited QueryMissing behavior. The broader split still needs
  shared dependency walking and a mutating/read-only helper boundary.

### T04 - Move Build Dedup Boundary To Scheduler Only

- `Status`: todo
- `Owner`: subagent-ok
- `Can delegate`: yes
- `Depends on`: T01
- `Validation`: focused scheduler tests for dedup by `.drv` path under
  `BuildMode.NORMAL`.
- `Commit`: can batch with T03 if small, otherwise separate.
- `Notes`: The request-local goal run may create many build requests, but active
  build dedup belongs in the singleton scheduler/build queue.

### T05 - Add SubstitutionQueue Skeleton Under Scheduler

- `Status`: done
- `Owner`: primary
- `Can delegate`: partial
- `Depends on`: T00
- `Validation`: unit tests for cache shape and queue construction; no real
  import yet.
- `Commit`: separate commit.
- `Notes`: `Scheduler` now owns an initial `SubstitutionQueue` skeleton with
  positive/negative TTL caches and per-store health logs. Global defaults are
  configured through `PynixdSettings`; per-store overrides are still pending.

### T06 - Implement Substitution Availability Cache And Health Logs

- `Status`: done
- `Owner`: subagent-ok
- `Can delegate`: yes
- `Depends on`: T05
- `Validation`: unit tests for positive TTL cache, negative TTL cache, per-store
  health log, sparse-cache behavior, and timeout/failure accounting.
- `Commit`: can batch with T05 if cohesive.
- `Notes`: Initial cache and health-log structures exist with the preferred
  `positive: TTLCache[StorePath, dict[StoreId, Result]]` and
  `negative: TTLCache[StorePath, dict[StoreId, Result]]` shape. Global TTL,
  cache-size, health-window, and query-timeout defaults live in
  `PynixdSettings`. Store-query success and failure are now recorded by
  `can_substitute`; per-store timeout overrides are still pending.

### T07 - Implement `can_substitute(path)`

- `Status`: done
- `Owner`: primary
- `Can delegate`: partial
- `Depends on`: T05, T06
- `Validation`: focused tests with fake stores proving first-positive return,
  only healthy stores block negative responses, all stores are still queried,
  and tasks remain strongly referenced until completion.
- `Commit`: separate commit if non-trivial.
- `Notes`: `can_substitute` now queries path info through Store APIs, returns
  first-positive availability and size metadata, waits only for healthy stores,
  and keeps background probe tasks strongly referenced until completion.

### T08 - Implement `get_substituter(path)` And `substitute(path)`

- `Status`: done
- `Owner`: primary
- `Can delegate`: partial
- `Depends on`: T07
- `Validation`: focused tests proving highest-priority source wins, active
  imports deduplicate by path, selected source path info is used for daemon
  streaming, and successful import makes later goals stop at local validity.
- `Commit`: separate commit.
- `Notes`: `get_substituter` now blocks on healthy substituters, uses cached
  positives where available, and chooses the highest-priority positive
  candidate. `substitute` deduplicates active imports by path and lets started
  imports finish. Per-store timeout overrides are still pending.

### T09 - Wire BuildPaths And BuildPathsWithResults Through Mutating GoalRun

- `Status`: done
- `Owner`: primary
- `Can delegate`: no
- `Depends on`: T03, T04, T08
- `Validation`: focused functional tests for simple build, cached local output,
  substitution before build, and multiple roots with continue-on-error behavior.
- `Commit`: separate commit.
- `Notes`: `SubstitutePathGoal` now delegates source selection and import
  deduplication to `Scheduler.substitution_queue` while keeping reference
  walking in the goal graph. `tests/functional/test_simple.py::test_store`
  passed after this handoff. Remaining work: explicit substitution-before-build
  coverage, subscriber cancellation, and broader result handling.

### T10 - Rework QueryMissing Through Read-Only GoalRun

- `Status`: done
- `Owner`: primary
- `Can delegate`: no
- `Depends on`: T03, T07
- `Validation`: focused functional tests for `will_build`, `will_substitute`,
  `unknown`, cached negative substitution, and first-positive size reporting.
- `Commit`: separate commit.
- `Notes`: QueryMissing now dispatches through a read-only goal entrypoint,
  parses flat `.drv` files, classifies known requested outputs by local validity
  and substitution availability, reports `will_substitute` with first-positive
  size metadata, and conservatively places dynamic/nested/deferred derivations in
  `will_build`. `tests/unit/test_query_missing_goal.py` and
  `tests/functional/test_queries.py::test_query_missing` pass.

### T11 - Review Result Model

- `Status`: done
- `Owner`: primary
- `Can delegate`: no
- `Depends on`: T03, T09, T10
- `Validation`: type checks and focused tests around dynamic path rewrites,
  resolved outputs, produced paths, and wire response conversion.
- `Commit`: batch with T09 or T10 only if the diff is small; otherwise separate.
- `Notes`: QueryMissing uses its own read-only plan result, so the mutating
  `GoalResult` shape remains scoped to build/substitution goals for now.
  Parent goals now copy child results before adding dynamic rewrite mappings or
  output-name aliases, avoiding request-local dedup aliasing. Focused pyright,
  goal result unit tests, substitution queue unit tests, QueryMissing unit tests,
  and `tests/functional/test_queries.py::test_query_missing` pass. This slice
  also fixed Pydantic forward-reference rebuilding for `PynixdSettings` and
  `LocalSocketStoreSpec`, which blocked adjacent settings-backed tests.

### T12 - Dynamic And Nested Derivation Review

- `Status`: todo
- `Owner`: primary
- `Can delegate`: no
- `Depends on`: T09, T10, T11
- `Validation`: focused dynamic-derivation tests and existing CA operation tests.
- `Commit`: separate commit if behavior changes.
- `Notes`: Confirm serial execution where one output discovers the next `.drv`,
  and parallel execution where dependency outputs are independent.

### T13 - Build Subscriber Cancellation Policy

- `Status`: todo
- `Owner`: subagent-ok
- `Can delegate`: yes, after primary defines expected behavior
- `Depends on`: T04, T09
- `Validation`: scheduler/build queue tests for unsubscribe, zero-subscriber
  cancellation of pending builds, and feasible cancellation of active builds.
- `Commit`: separate commit.
- `Notes`: Builds should continue while there are active subscribers. If all
  clients disconnect, cancellation is preferred when architecturally feasible.

### T14 - Documentation And Glossary Update

- `Status`: todo
- `Owner`: subagent-ok
- `Can delegate`: yes
- `Depends on`: major architecture choices implemented or stable
- `Validation`: docs-only review.
- `Commit`: batch with the final implementation commit or separate docs commit.
- `Notes`: Update `GLOSSARY.md` for terms such as `GoalRun`,
  `SubstitutionQueue`, read-only planning goal, mutating ensure goal, and
  scheduler work lane.

### T15 - Final Validation

- `Status`: todo
- `Owner`: primary
- `Can delegate`: no
- `Depends on`: T09, T10, T11, T12, T13
- `Validation`: run `direnv exec . just precommit` with full output visible.
- `Commit`: commit or squash only after reviewing `jj --no-pager diff`.
- `Notes`: If `just precommit` fails from unrelated existing issues, capture the
  exact failures and run the narrowest focused checks that prove goal-system
  behavior.

## Open Questions

1. Should `can_substitute(path)` return only boolean plus size metadata, or a
   richer availability object that names the first responding store for
   diagnostics?
2. What exact per-store config key should override the global substitution query
   timeout?
3. Should zero-subscriber active builds be cancelled immediately, or after a
   short grace period to tolerate reconnects?
4. Should `QueryMissing` update substitution availability caches even though it
   is otherwise read-only? Current decision: yes, path-info cache updates are
   allowed because they do not mutate the Nix store.
5. Should the older `docs/design/goal-index.md` be replaced, deleted, or marked
   historical after the request-local design lands?

## Handoff Checklist For Subagents

When delegating a task, include:

- the task ID from this file;
- the exact files or modules in scope;
- whether tests may be added or edited;
- the maximum validation command allowed;
- a reminder that subagents must not run `jj` or `git`;
- a request for a concise summary of changed files and validation results.

Subagents should stop and report back if they encounter an architecture fork,
unexpected dirty files, inconsistent tool output, or a failure that requires
changing task scope.
