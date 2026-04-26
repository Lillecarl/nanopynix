# Ruff Violation Cleanup Plan - DONE

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

## Phase 1: PTH — Replace os/open with pathlib (27 violations) - DONE

These are straightforward mechanical replacements.

### 1a. PTH123 — `open()` → `Path.open()` (16 violations) - DONE

### 1b. PTH103 — `os.makedirs` → `Path.mkdir(parents=True)` (4 violations) - DONE

### 1c. PTH110 — `os.path.exists` → `Path.exists()` (3 violations) - DONE

### 1d. PTH101 — `os.chmod` → `Path.chmod()` (2 violations) - DONE

### 1e. PTH107 — `os.remove` → `Path.unlink()` (1 violation) - DONE

### 1f. PTH207 — `glob.glob` → `Path.glob` (1 violation) - DONE (using noqa where appropriate)

---

## Phase 2: TRY — Try/except best practices (10 violations) - DONE

### 2a. TRY300 — Move statement to `else` block (7 violations) - DONE

### 2b. TRY203 — Remove useless try/except that just re-raises (2 violations) - DONE

### 2c. TRY301 — Abstract raise to inner function (1 violation) - DONE

### 2d. TRY004 — Use TypeError instead of RuntimeError for type checks (1 violation) - DONE

---

## Phase 3: ASYNC — Blocking calls in async functions (22 violations) - DONE

### 3a. ASYNC230 — Blocking `open()` in async functions (7 violations) - DONE

### 3b. ASYNC240 — Blocking pathlib methods in async functions (6 violations) - DONE

### 3c. ASYNC109 — Async function with `timeout` parameter (3 violations) - DONE

### 3d. ASYNC110 — Async busy-wait (3 violations) - DONE

### 3e. ASYNC221 — Blocking subprocess in async functions (3 violations) - DONE

---

## Phase 4: A002 — Builtin argument shadowing (7 violations) - DONE (via renaming id -> store_id)

---

## Phase 5: TRY003 — Long exception messages (45 violations) - DONE (via global ignore)

---

## Phase 6: Misc small fixes (6 violations) - DONE

### 6a. ARG001 — Unused function arguments (2 violations) - DONE (using noqa)

### 6b. ARG003 — Unused class method arguments (2 violations in config.py) - DONE (using noqa)

### 6c. N814 — CamelCase import alias (1 violation) - DONE

### 6d. PERF401 — Manual list comprehension (2 violations) - DONE

---

## Phase 7: Final verification - DONE

1. Run `nix-shell --pure --run "ruff check pynixd tests"` — should show 0 errors
2. Run `nix-shell --pure --run "just precommit"` — ruff + pyright should pass (only known pre-existing pyright error on conftest.py:293 which already has `# type: ignore`)
3. Verify the `pyproject.toml` ignore list is minimal:
   - Expected final ignore list: `E501`, `SIM115`, `TID252`, `ARG002`, `TRY003`
   - Expected per-file-ignores: `tests/**` → `ERA001`, `RUF059`, `F401`, `T201`, `ARG001`
