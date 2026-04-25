# Ruff Violation Cleanup Plan

**Goal**: Eliminate all 119 remaining ruff violations. Commit after every logical group. Run `nix-shell --pure --run "ruff check pynixd tests"` and `nix-shell --pure --run "just precommit"` to verify after each commit.

**Working directory**: `/home/lillecarl/Code/pynixd`

**VCS**: Use `jj` (Jujutsu), NOT git. Always include `--no-pager`. Use `jj commit -m "..."` to finish tasks.

**CRITICAL RULES**:
1. Do NOT add global ignores unless there is a truly legitimate reason documented with a comment. The user explicitly wants minimal ignores.
2. If a rule violation is due to domain terminology (e.g. `id` in a Store), prefer per-line `# noqa: RULE` with a brief comment, not a global ignore.
3. Commit frequently — one commit per logical group of fixes.
4. Run `nix-shell --pure --run "just precommit"` after each commit to verify ruff + pyright pass. The pyright error on conftest.py:293 is pre-existing and already has a `# type: ignore`.
5. Do NOT add comments to code unless asked.

---

## Phase 1: PTH — Replace os/open with pathlib (27 violations)

These are straightforward mechanical replacements.

### 1a. PTH123 — `open()` → `Path.open()` (16 violations)

Replace `with open(path) as f:` with `with path.open() as f:`. For binary mode: `with path.open("rb") as f:`.

| File | Line | Current | Replacement |
|------|------|---------|-------------|
| `pynixd/config.py` | 206 | `with open(path) as f:` | `with path.open() as f:` |
| `pynixd/drv_parser.py` | 689 | `with open(fs_path) as f:` | `with fs_path.open() as f:` |
| `pynixd/http_server.py` | 293 | `with open(temp_path, "wb") as f:` | `with temp_path.open("wb") as f:` (also ASYNC230 — see note) |
| `pynixd/http_server.py` | 371 | `dctx.stream_reader(open(path, "rb"))` | `dctx.stream_reader(path.open("rb"))` |
| `pynixd/http_server.py` | 376 | `with open(path, "rb") as bf:` | `with path.open("rb") as bf:` |
| `pynixd/http_server.py` | 384 | `ctx = open(path, "rb")` | `ctx = path.open("rb")` |
| `pynixd/monitor.py` | 347 | `with open(path) as f:` | `with Path(path).open() as f:` (path is `str`, wrap with `Path()`) |
| `pynixd/monitor.py` | 377 | `with open(f"/proc/pressure/{p}") as f:` | `with Path(f"/proc/pressure/{p}").open() as f:` |
| `pynixd/monitor.py` | 384 | `with open("/proc/meminfo") as f:` | `with Path("/proc/meminfo").open() as f:` |
| `pynixd/monitor.py` | 409 | `with open(path) as f:` | `with Path(path).open() as f:` (path is `str`, wrap with `Path()`) |
| `tests/ai/deferred_resolve.py` | 495 | `with open(nix_aterm_path) as f:` | `with nix_aterm_path.open() as f:` |
| `tests/ai/deferred_resolve.py` | 578 | `with open(resolved_drv_fs_path, "w") as f:` | `with resolved_drv_fs_path.open("w") as f:` |
| `tests/ai/deferred_resolve.py` | 622 | `with open(out_fs) as f:` | `with out_fs.open() as f:` |
| `tests/conftest.py` | 414 | `with open(profile_file, "w") as f:` | `with profile_file.open("w") as f:` (ensure `profile_file` is `Path`) |
| `tests/conftest.py` | 431 | `with open(log_file, "a") as f:` | `with log_file.open("a") as f:` (ensure `log_file` is `Path`) |
| `tests/functional/test_psi_parsers.py` | 262 | `with open(path) as f:` | `with Path(path).open() as f:` (path is `str`, wrap) |

**Note on monitor.py `path` param**: The `local_read` and `local_exists` functions take `path: str`. For PTH123, wrap with `Path(path)` when calling `.open()`. Also consider changing the parameter type to `Path` where possible (check callers).

**Note on http_server.py:293**: This also triggers ASYNC230 (blocking open in async function). After converting to `Path.open()`, the ASYNC230 violation remains because `Path.open()` is still blocking. Add `# noqa: ASYNC230` on that line with a comment like: `# noqa: ASYNC230 — uses run_in_executor for writes`.

### 1b. PTH103 — `os.makedirs` → `Path.mkdir(parents=True)` (4 violations)

| File | Line | Current | Replacement |
|------|------|---------|-------------|
| `pynixd/store/local.py` | 109 | `os.makedirs(socket_dir, exist_ok=True)` | `socket_dir.mkdir(parents=True, exist_ok=True)` |
| `tests/benchmark/test_bench_nar.py` | 104 | `os.makedirs(BENCH_DST, exist_ok=True)` | `BENCH_DST.mkdir(parents=True, exist_ok=True)` (BENCH_DST may need to be `Path`) |
| `tests/benchmark/test_bench_pynixd.py` | 199 | `os.makedirs(managed_path, exist_ok=True)` | `managed_path.mkdir(parents=True, exist_ok=True)` |
| `tests/functional/test_http_cache.py` | 150 | `os.makedirs(subst_store_path, exist_ok=True)` | `subst_store_path.mkdir(parents=True, exist_ok=True)` |

After fixing, check if `import os` can be removed from these files.

### 1c. PTH110 — `os.path.exists` → `Path.exists()` (3 violations)

| File | Line | Current | Replacement |
|------|------|---------|-------------|
| `pynixd/monitor.py` | 351 | `return os.path.exists(path)` | `return Path(path).exists()` |
| `pynixd/monitor.py` | 413 | `return os.path.exists(path)` | `return Path(path).exists()` |
| `pynixd/monitor.py` | 415 | `if os.path.exists("/sys/fs/cgroup/cpu.pressure"):` | `if Path("/sys/fs/cgroup/cpu.pressure").exists():` |

**Also fixes ASYNC240** on lines 351 and 413 (same locations). Add `# noqa: ASYNC240` on the `local_exists` function definitions since `Path.exists()` is still blocking in an async context.

### 1d. PTH101 — `os.chmod` → `Path.chmod()` (2 violations)

| File | Line | Current | Replacement |
|------|------|---------|-------------|
| `tests/conftest.py` | 456 | `os.chmod(path, stat.S_IWRITE \| stat.S_IREAD \| stat.S_IEXEC)` | `Path(path).chmod(stat.S_IWRITE \| stat.S_IREAD \| stat.S_IEXEC)` |
| `tests/conftest.py` | 467 | `os.chmod(path, stat.S_IWRITE \| stat.S_IREAD)` | `Path(path).chmod(stat.S_IWRITE \| stat.S_IREAD)` |

Note: `path` in `handle_errors(func, path, _excinfo)` is from `shutil.rmtree` callback — it's a `str`. Wrap with `Path()`.

### 1e. PTH107 — `os.remove` → `Path.unlink()` (1 violation)

| File | Line | Current | Replacement |
|------|------|---------|-------------|
| `pynixd/http_server.py` | 433 | `os.remove(nar_temp_path)` | `nar_temp_path.unlink()` (ensure `nar_temp_path` is `Path` — it likely already is) |

### 1f. PTH207 — `glob.glob` → `Path.glob` (1 violation)

| File | Line | Current | Replacement |
|------|------|---------|-------------|
| `tests/conftest.py` | 477 | `for path in glob.glob(pattern):` | `for path in Path().glob(pattern):` — OR use `Path(pattern).parent.glob(Path(pattern).name)` if pattern is a full path with glob. Read the function to understand the pattern format first. |

**Commit after Phase 1**: `style: replace os/open with pathlib equivalents (PTH123 PTH103 PTH110 PTH101 PTH107 PTH207)`

---

## Phase 2: TRY — Try/except best practices (10 violations)

### 2a. TRY300 — Move statement to `else` block (7 violations)

When code after a `try` block only runs if no exception occurred, move it into the `else` clause.

| File | Line | Fix |
|------|------|-----|
| `pynixd/local_store_db.py` | 276 | Move `return instance` into `else:` block after `except Exception as e:` |
| `pynixd/operations/probe_systems.py` | 150 | Move `return name, accepted` into `else:` after `except Exception as e:` |
| `pynixd/operations/query_all_valid_paths.py` | 93 | Move `return resp` into `else:` after `except Exception as e:` |
| `pynixd/store/local.py` | 167 | Move `self.daemon_ready.set()` and `return` into `else:` after `except (ConnectionRefusedError, ConnectionResetError):` |
| `pynixd/store/ssh.py` | 90 | Move `return True` after `await sftp.stat(path)` into `else:` after `except asyncssh.SFTPError:` |
| `pynixd/store/ssh.py` | 180 | Move `return self.conn` into `else:` after `except Exception:` |
| `tests/ai/deferred_replay.py` | 77 | Move `return None` into `else:` after `except Exception as e:` |

### 2b. TRY203 — Remove useless try/except that just re-raises (2 violations)

These `except ... : raise` blocks add no value — remove the try/except wrapper.

| File | Line | Fix |
|------|------|-----|
| `pynixd/store/ssh.py` | 246 | Remove `try: / except Exception: raise` wrapper around `ssh_conn = await self.ensure_ssh()` |
| `pynixd/store/ssh.py` | 322 | Same pattern — remove try/except wrapper |

**Careful**: Read the full context. If the try/except exists to establish an error boundary or there's cleanup code, keep it. Only remove if it literally just does `except X: raise` with nothing else.

### 2c. TRY301 — Abstract raise to inner function (1 violation)

| File | Line | Fix |
|------|------|-----|
| `pynixd/monitor.py` | 292 | The `raise PermissionError(...)` inside a `try` block. Move the permission check + raise before the try block, or extract to a helper. The simplest fix: move the `if not os.access(...): raise PermissionError(...)` to BEFORE the `try:` block. |

### 2d. TRY004 — Use TypeError instead of RuntimeError for type checks (1 violation)

| File | Line | Fix |
|------|------|-----|
| `tests/functional/test_persistence.py` | 45 | Change `raise RuntimeError("QueryAllValidPaths not supported")` to `raise TypeError("QueryAllValidPaths not supported")`. — Wait, actually this is an "unsupported operation" error, not a type error. This is a false positive from TRY004. Add `# noqa: TRY004` since this is a protocol-level rejection, not a type check. |

**Commit after Phase 2**: `style: fix try/except patterns (TRY300 TRY203 TRY301 TRY004)`

---

## Phase 3: ASYNC — Blocking calls in async functions (22 violations)

### 3a. ASYNC230 — Blocking `open()` in async functions (7 violations)

After Phase 1 converts these to `Path.open()`, they'll still be "blocking." Add per-line `# noqa: ASYNC230` with explanation comments where the blocking is acceptable:

| File | Line | Reasoning |
|------|------|-----------|
| `pynixd/http_server.py` | 293 | Uses `run_in_executor` for writes — acceptable. Add `# noqa: ASYNC230` |
| `pynixd/monitor.py` | 347 | `/proc` reads are instant, kernel-backed — acceptable. Add `# noqa: ASYNC230` |
| `pynixd/monitor.py` | 377 | Same — `/proc/pressure` reads. Add `# noqa: ASYNC230` |
| `pynixd/monitor.py` | 384 | `/proc/meminfo` read. Add `# noqa: ASYNC230` |
| `pynixd/monitor.py` | 409 | Same — local `local_read` helper. Add `# noqa: ASYNC230` |
| `tests/ai/deferred_resolve.py` | 495, 578, 622 | Test code, not performance-critical. Already in per-test ignore for ASYNC230? Check — these are in `tests/ai/` which may not be covered by `tests/**` glob. Add per-line `# noqa: ASYNC230` |
| `tests/conftest.py` | 414 | Already covered by `tests/**` glob — no action needed |

### 3b. ASYNC240 — Blocking pathlib methods in async functions (6 violations)

After Phase 1 converts `os.path.exists` to `Path.exists()`, these still trigger ASYNC240. Add per-line `# noqa: ASYNC240`:

| File | Line | Fix |
|------|------|-----|
| `pynixd/monitor.py` | 351 | `return Path(path).exists()` — add `# noqa: ASYNC240` (instant local FS check) |
| `pynixd/monitor.py` | 413 | Same |
| `pynixd/ssh_server.py` | 75 | `host_key_path.exists()` — add `# noqa: ASYNC240` (one-time startup check) |
| `pynixd/unix_server.py` | 69 | `socket_path.exists()` — add `# noqa: ASYNC240` |
| `pynixd/unix_server.py` | 70 | `socket_path.unlink()` — add `# noqa: ASYNC240` (one-time cleanup) |
| `tests/functional/test_nix_integration_unix.py` | 56 | `expr_path.write_text(...)` — add `# noqa: ASYNC240` (test setup) |

### 3c. ASYNC109 — Async function with `timeout` parameter (3 violations)

These use `asyncio.wait_for(..., timeout=timeout)` which is the standard Python pattern. The rule wants `asyncio.timeout()` context manager (Python 3.11+). However, `asyncio.wait_for` is perfectly fine and more readable for waiting on Events. Add per-line `# noqa: ASYNC109`:

| File | Line | Method |
|------|------|--------|
| `pynixd/monitor.py` | 50 | `wait_cpu_clear` — add `# noqa: ASYNC109` on the `def` line |
| `pynixd/monitor.py` | 59 | `wait_mem_clear` — add `# noqa: ASYNC109` |
| `pynixd/monitor.py` | 68 | `wait_io_clear` — add `# noqa: ASYNC109` |

### 3d. ASYNC110 — Async busy-wait (3 violations)

These are long-running monitor loops, not busy-waits. Add per-line `# noqa: ASYNC110`:

| File | Line | Fix |
|------|------|-----|
| `pynixd/instance.py` | 359 | Add `# noqa: ASYNC110` — long-running server keep-alive loop |
| `pynixd/monitor.py` | 127 | Add `# noqa: ASYNC110` — 60s PSI polling interval |
| `tests/functional/test_local_psi_gating.py` | 29 | Add `# noqa: ASYNC110` — test mock |

### 3e. ASYNC221 — Blocking subprocess in async functions (3 violations)

All in test/benchmark code. Add per-line `# noqa: ASYNC221`:

| File | Line |
|------|------|
| `tests/benchmark/test_bench_nar.py` | 117 |
| `tests/benchmark/test_bench_nar.py` | 128 |
| `tests/benchmark/test_bench_pynixd.py` | 86 |

**Commit after Phase 3**: `style: add per-line ASYNC noqa for acceptable blocking patterns (ASYNC109 ASYNC110 ASYNC221 ASYNC230 ASYNC240)`

---

## Phase 4: A002 — Builtin argument shadowing (7 violations)

**`id` is domain terminology** in pynixd (every Store has an `id`). Renaming to `store_id` or `id_` would be a massive refactor touching every caller. Use per-line `# noqa: A002` for `id` params.

| File | Line | Builtin | Fix |
|------|------|---------|-----|
| `pynixd/store/base.py` | 72 | `id` | `# noqa: A002` — domain terminology |
| `pynixd/store/local.py` | 43 | `id` | `# noqa: A002` |
| `pynixd/store/ssh.py` | 220 | `id` | `# noqa: A002` |
| `pynixd/store/ssh.py` | 299 | `id` | `# noqa: A002` |
| `tests/conftest.py` | 512 | `print` | Rename `print` → `verbose` or `log_output` (this one IS worth renaming — `print` is not domain terminology) |
| `tests/functional/mock_store.py` | 59 | `id` | `# noqa: A002` |
| `tests/functional/mock_store.py` | 134 | `id` | `# noqa: A002` |

For `tests/conftest.py:512`: the `print` parameter in `run_subproc` should be renamed to `verbose` (or `log_output`). Search all callers of `run_subproc` and update the keyword argument.

**Commit after Phase 4**: `style: add per-line A002 noqa for id params, rename print→verbose in conftest`

---

## Phase 5: TRY003 — Long exception messages (45 violations)

**This is the largest group.** TRY003 wants inline exception messages moved to the exception class `__init__` or a custom exception subclass. This is debatable — creating a custom exception class for every `raise ValueError(f"...")` is excessive.

**Decision: Add TRY003 to the global ignore list.** Here's the rationale:
- 45 violations across the codebase
- Most are `raise ValueError(f"...")` or `raise RuntimeError("...")` with contextual messages
- Creating custom exception subclasses for each would double the code size with no runtime benefit
- The tryceratops docs themselves say this rule is "strict" and many projects ignore it
- The messages provide useful debugging context that would be lost if moved to class-level constants

Add to `pyproject.toml` ignore list:
```toml
"TRY003",  # long exception messages are acceptable — custom subclasses would be excessive
```

**Commit after Phase 5**: `chore: ignore TRY003 — long exception messages are acceptable without custom subclasses`

---

## Phase 6: Misc small fixes (6 violations)

### 6a. ARG001 — Unused function arguments (2 violations)

| File | Line | Param | Fix |
|------|------|-------|-----|
| `pynixd/ssh_server.py` | 53 | `stores` | Prefix with underscore: `_stores` — BUT check if this param is part of a callback/uniform interface. If it matches a pattern like `start_ssh_server(stores, local_store, scheduler)` where `start_unix_server` also takes `stores`, rename to `_stores` in both places for consistency. |
| `pynixd/unix_server.py` | 30 | `stores` | Same — rename to `_stores` |

### 6b. ARG003 — Unused class method arguments (2 violations in config.py)

| File | Line | Param | Fix |
|------|------|-------|-----|
| `pynixd/config.py` | 270 | `dotenv_settings` | Prefix with underscore: `_dotenv_settings` |
| `pynixd/config.py` | 271 | `file_secret_settings` | Prefix with underscore: `_file_secret_settings` |

These are Pydantic settings source method overrides with a fixed signature.

### 6c. N814 — CamelCase import alias (1 violation)

| File | Line | Fix |
|------|------|-----|
| `tests/ai/deferred_resolve.py` | 613 | `QueryDerivationOutputMapRequest as QDOM` — rename to `QueryDerivationOutputMapRequest as QdomRequest` or just use the full name. Add `# noqa: N814` if the abbreviation is intentional and readable. |

### 6d. PERF401 — Manual list comprehension (2 violations)

| File | Line | Fix |
|------|------|-----|
| `pynixd/types/path_info.py` | 171 | Replace `for sig in sorted(self.sigs): lines.append(f"Sig: {sig}")` with `lines.extend(f"Sig: {sig}" for sig in sorted(self.sigs))` |
| `tests/functional/mock_store.py` | 244 | Replace the `infos.append(ValidPathInfo(...))` loop with `infos = [ValidPathInfo(path=p, ...) for p in request.paths]` |

**Commit after Phase 6**: `style: fix misc violations (ARG001 ARG003 PERF401 N814)`

---

## Phase 7: Final verification

1. Run `nix-shell --pure --run "ruff check pynixd tests"` — should show 0 errors
2. Run `nix-shell --pure --run "just precommit"` — ruff + pyright should pass (only known pre-existing pyright error on conftest.py:293 which already has `# type: ignore`)
3. Verify the `pyproject.toml` ignore list is minimal:
   - Expected final ignore list: `E501`, `SIM115`, `TID252`, `ARG002`, `TRY003`
   - Expected per-file-ignores: `tests/**` → `ERA001`, `RUF059`, `F401`, `T201`, `ARG001`

---

## Summary of Global Ignores (after all phases)

```toml
ignore = [
    "E501",      # line length — we use line-length=120
    "SIM115",    # generator file handles — open then wrapped with `with ctx as f:`
    "TID252",    # relative imports — by design within packages
    "ARG002",    # unused method args — protocol interfaces have uniform signatures
    "TRY003",    # long exception messages — custom subclasses would be excessive
]
```

## Summary of Per-Line Noqas Added

| Rule | Count | Locations |
|------|-------|-----------|
| ASYNC230 | 5 | http_server.py, monitor.py (3x), deferred_resolve.py |
| ASYNC240 | 5 | monitor.py (2x), ssh_server.py, unix_server.py (2x), test_nix_integration_unix.py |
| ASYNC109 | 3 | monitor.py (3x) |
| ASYNC110 | 3 | instance.py, monitor.py, test_local_psi_gating.py |
| ASYNC221 | 3 | test_bench_nar.py (2x), test_bench_pynixd.py |
| A002 | 6 | store/base.py, store/local.py, store/ssh.py (2x), mock_store.py (2x) |
| TRY004 | 1 | test_persistence.py |
| N814 | 1 | deferred_resolve.py |