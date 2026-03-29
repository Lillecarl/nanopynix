# pynixd Code Quality Improvement Plan

## Context

The user requested an analysis of all Python code in pynixd to find issues: filenames, class names, variable names, duplicated code, dead code, stupid code, and antipatterns. A thorough codebase analysis was performed.

---

## Issues by Severity

### Critical (Likely Bugs)

1. **`drv_parser.py:425-452` - `collect_required_paths` ignores `dynamic_input_drvs`**
   - `DrvWithVersion` format populates `dynamic_input_drvs`, not `input_drvs`
   - This function only recurses through `input_drvs`, missing all dynamic derivation inputs
   - Currently unused, but should handle dynamic derivations for future use
   - **Fix**: Add handling for `parsed.dynamic_input_drvs` in the collection loop

2. **`stderr.py:239` - Fragile `LAST = object()` sentinel**
   - Using bare `object()` as sentinel could accidentally compare equal
   - **Fix**: Use a dedicated class or `type()` sentinel

### High Priority (Fragile/Hacky)

3. **`backend.py:122-124` - Accessing `Semaphore._value` private attribute**
   - `self._max_builds - self._build_semaphore._value` relies on CPython internals
   - **Fix**: Track in-flight count explicitly with a counter

4. **`backend.py:184-194` - `_reader_is_dirty` accesses private stream buffers**
   - Directly reads `_buffer` and `_recv_buf` from asyncio/asyncssh internals
   - **Fix**: Wrap reader in a tracking class or use a different approach

5. **`scheduler.py:208` - `transferring` list may be incomplete**
   - `transferring.append()` is inside a conditional that may not fire
   - The `transferring` list at line 216 could miss builds that started transferring
   - **Fix**: Ensure all transfers are tracked in the `transferring` list

6. ~~**`operations/base.py:679` - `write_framed` has unused `chunk_size` parameter**~~ → **WONTFIX**
   - The `chunk_size` parameter IS used internally (passed to `FramedWriter`)
   - C++ Nix doesn't chunk at 64kb, but Python's `FramedWriter` chunks for bounded memory
   - Keeping flexibility to write bigger frames for performance tuning

### Medium Priority (Code Quality)

7. **`wire.py:135-316` - `copy_nar` and `stream_nar` duplicate token-parsing logic**
   - Both functions have nearly identical `_fwd_token` inner function logic
   - **Fix**: Extract shared `_forward_token` helper

8. **`store_mutations.py:45-156` - `forward` methods repeat identical patterns**
   - `AddToStoreRequest.forward` and `AddToStoreNarRequest.forward` are nearly identical
   - **Fix**: Extract common `forward` helper or base class

9. **`proxy.py:389` - Mutation of dataclass field `request.derivation._is_dynamic`**
   - Mutating `_is_dynamic` (a `repr=False` field) after construction breaks dataclass invariants
   - **Fix**: Set the field at construction time or redesign

10. **`proxy.py:70` - `NIX_VERSION = "pynixd-0.1.0"` misnamed**
    - All caps suggests a Nix version constant; should be `PYNIXD_VERSION`
    - **Fix**: Rename to `PYNIXD_VERSION`

### Low Priority (Naming/Cleanup)

11. **`stderr.py` filename** - `stderr.py` is vague; could be `wire_stderr.py` or `daemon_messages.py`

12. **`_BufWriter` in `stderr.py:376`** - Name doesn't convey that it's a sync BytesIO wrapper for wire writes

13. **`KeyedBuildResult` in `operations/builds.py:40`** - Name is odd; `BuildPathResult` would be clearer

14. **`store.py:_op_log`** - Unbounded growth in long-running processes; should be capped or removed

15. **`NarFromPathResponse` in `operations/queries.py:171-184`** - Buffers entire NAR in memory unnecessarily (though `nar_from_path_streaming` exists but isn't used)

16. **`ssh_server.py:25-40` - Accepts all auth** - Dev-only but no warning; security risk if deployed accidentally

17. **`is_non_deterministic: int` in `operations/base.py:536`** - Should be `bool` for clarity

18. **`cam` abbreviation in `store_mutations.py`** - Single-letter field name; use `content_address_method`

---

## Recommended Order

1. Fix `collect_required_paths` bug (critical - data loss risk with dynamic derivations)
2. Fix `LAST` sentinel (simple, prevents future bugs)
3. Fix `_reader_is_dirty` / semaphore `_value` (fragile CPython assumptions)
4. Fix `scheduler.py` transferring list tracking
6. Deduplicate `copy_nar`/`stream_nar`
7. Deduplicate `forward` methods in store_mutations
8. Rename `NIX_VERSION` → `PYNIXD_VERSION`
9. Fix `is_non_deterministic` typing
10. Fix dataclass mutation antipattern in proxy.py
11. Rename `_BufWriter` and consider renaming `stderr.py`
12. Cap or remove unbounded `_op_log`
13. Review `NarFromPathResponse` memory buffering (performance)

---

## Files to Modify

- `pynixd/drv_parser.py` - fix `collect_required_paths`
- `pynixd/stderr.py` - fix `LAST` sentinel, rename `_BufWriter`
- `pynixd/backend.py` - fix semaphore access, `_reader_is_dirty`
- `pynixd/operations/base.py` - fix `is_non_deterministic` type
- `pynixd/scheduler.py` - fix `transferring` tracking
- `pynixd/wire.py` - deduplicate nar functions
- `pynixd/store_mutations.py` - deduplicate forward methods
- `pynixd/proxy.py` - fix version constant, dataclass mutation
- `pynixd/operations/queries.py` - review NAR buffering
- `pynixd/ssh_server.py` - add dev mode warning
