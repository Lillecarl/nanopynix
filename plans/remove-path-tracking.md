# Plan: Remove Path Tracking

## Why

Path tracking (the `PathTracker` / `PathTrackerInstance` system) is too complex for its usefulness. Its primary purpose is to let the **scheduler** skip a `QueryValidPaths` round-trip by checking an in-memory `known_paths` set. The maintenance burden — DB persistence, sync on start, propagation through every operation handler, cross-store correctness — outweighs this single optimization. The scheduler can just call `QueryValidPaths` when it needs to know if a store has a path; the daemon protocol already handles this efficiently. `LocalDBStore` should continue updating `registrationTime` in the DB but that is separate from path tracking.

## What gets removed

1. **`pynixd/path_tracker.py`** — the entire `PathTracker` and `PathTrackerInstance` classes
2. **`Store.tracker`** attribute (base.py:62)
3. **`Store.has_path()`, `Store.has_all_paths()`, `Store.count_common_paths()`** (base.py:183-189)
4. **`PynixdContext.path_tracker`** (context.py, instance.py)
5. **`Server.path_tracker`** property (instance.py:110-111)
6. **`sync_paths()`** in `DaemonStore` — the entire method (daemon.py:427-461)
7. All `store.tracker.add_known_path()` calls in operations handlers — these become no-ops or simply removed
8. All `store.tracker.has_path()` calls — replace with `store.execute(IsValidPathRequest(...))`
9. `LocalStoreDB.mark_known_paths()` and `pending_known_paths` / `pending_removed_known_paths` — remove (keep only `pending_regtime` and `mark_path`)
10. DB table `PynixdKnownPaths` and all its queries
11. `LocalStoreDB.get_known_paths()` — remove

## What gets replaced in the scheduler

### `scheduler.py:validate_known_paths` (lines 431-451)
Currently: queries `QueryValidPaths` for unknown paths, updates tracker.
**Replace with**: just call `QueryValidPaths` directly. No caching needed.

### `scheduler.py:287` — local fast-track check
Currently: `if self.local_store.tracker.has_all_paths(build.request.derivation.input_srcs):`
**Replace with**: always perform the fast-track attempt (the `execute_build` call on local store). The first `QueryValidPaths` call in the build path will naturally discover missing inputs.

### `scheduler.py:513` — `_prepare_build` missing inputs
Currently: `if p not in store.tracker.known_paths`
**Replace with**: always stream inputs unconditionally, or do a single `QueryValidPaths` batch check.

### `scheduler.py:588` — `_collect_outputs`
Currently: `store.tracker.add_known_paths(all_output_paths)`
**Replace with**: nothing — no caching needed. The daemon already knows these paths.

### `allocator.py:84` — data locality scoring
Currently: `store.tracker.count_common_paths(input_srcs)` — awards ranking points for stores that already have the inputs.
**Replace with**: remove this scoring factor entirely, or replace with a scheduler-local heuristic if needed. The data locality bonus can be simplified or dropped.

## What gets replaced in `LocalDBStore`

- `local_db.py:26-27` — `PathTracker(db=self.db)` and `create_instance()` — remove.
- All `self.tracker.add_known_path()` calls — remove (the DB methods handle persistence separately).
- `self.tracker.has_path()` calls — replace with DB queries or `IsValidPathRequest`.

## What gets replaced in handlers

- `add_to_store.py:133`, `add_multiple_to_store.py:91`, `add_to_store_nar.py:104` — `tracker.add_known_path()`
- `build_paths.py:81,101,188`, `build_derivation.py:78` — `tracker.add_known_path()`
- `collect_garbage.py:130` — `tracker.remove_known_paths()`
- `query_closure_with_info.py:137`, `query_path_infos.py:139`, `query_closure.py:90,94`, `query_valid_paths.py:90,94`, `query_path_from_hash_part.py:75,80`, `query_path_info.py:88,113,119`, `is_valid_path.py:71,78,83`, `ca_derivations.py:81,153`, `query_derivation_output_map.py:75`

All of these become **no-ops or removed**. The daemon already knows what paths it has; the tracker was a redundant cache.

## What gets replaced in `store/transfer.py`

- `transfer.py:65` — filter `not in dst.tracker.known_paths`
**Replace with**: `QueryValidPathsRequest` on dst store to check which paths are missing.
- `transfer.py:139` — `dst.tracker.add_known_paths()`
**Replace with**: nothing.

## What gets replaced in `DaemonStore`

- `daemon.py:292` — `if sync_paths: await self.sync_paths()` in `start()`
**Replace with**: nothing — remove the sync_paths call entirely.
- `daemon.py:427-461` — entire `sync_paths()` method — remove.
- `daemon.py:292` call site — remove.

## What gets replaced in `instance.py`

- `instance.py:71` — `PathTracker(db=None)` → remove.
- `instance.py:77` — `path_tracker=path_tracker` → remove from context.
- `instance.py:110-111` — `path_tracker` property → remove.
- `instance.py:120-131` — add_store tracker setup → remove.
- `instance.py:264,271` — `ctx.path_tracker.db = ...` → remove.
- `instance.py:274-277` — `local_store.tracker = self.ctx.path_tracker.create_instance(...)` → remove.

## What gets replaced in `context.py`

- `PynixdContext.path_tracker` — remove the field entirely.

## What gets replaced in `Store` base class

- `base.py:18` — `PathTrackerInstance` import → remove.
- `base.py:62` — `self.tracker = PathTrackerInstance(...)` → remove.
- `base.py:183-189` — `has_path()`, `has_all_paths()`, `count_common_paths()` → remove.

## Test impact

- Session fixture `tests/_conftest/config.py` — remove `path_tracker` usage in `Server` construction.
- Any test calling `store.tracker.add_known_path()` — replace with direct daemon protocol calls or remove assertions.
- `test_persistence.py` — entire test file for persistence of known paths becomes obsolete and should be removed.
- `tests/functional/mock_store.py` — remove `tracker` references from MockStore.

## Order of operations

1. Remove `sync_paths()` from `DaemonStore.start()`
2. Delete `DaemonStore.sync_paths()` method
3. Remove `PathTracker`/`PathTrackerInstance` from `Store.__init__` and base properties
4. Fix scheduler: replace tracker usage with direct wire ops
5. Fix allocator: drop data locality scoring or simplify
6. Fix `store/transfer.py`: use wire ops instead of tracker
7. Remove all handler `tracker.add_known_path()` calls
8. Remove `LocalStoreDB` path-tracking (keep regtime)
9. Remove `PynixdContext.path_tracker`
10. Remove `instance.py` path_tracker setup
11. Delete `pynixd/path_tracker.py`
12. Delete `DB_PynixdKnownPaths` table and queries
13. Delete `test_persistence.py`
14. Fix remaining test references
