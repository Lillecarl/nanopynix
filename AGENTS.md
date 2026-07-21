# Useful commands
Note: Current full pytest invocations takes almost 600 seconds because it runs the test suite both in single-store and daemon backend mode serially
- direnv exec . timeout 500 pytest tests
- direnv exec . timeout 500 pytest tests --cov --cov-report=term-missing --cov-report= # coverage report, including the multiprocessing-forkserver Nix worker subprocess (see tests/conftest.py's _enable_subprocess_coverage and tests/_coverage_subprocess/sitecustomize.py)
- direnv exec . pyright
- direnv exec . ruff check --fix
- direnv exec . ruff check --config ruff-strict.toml --fix

# Version control

This repository uses Jujutsu (`jj`) for version control. Prefer `jj` commands
for status, diffs, history, and commit/change inspection. Do not assume a Git
workflow or run Git porcelain commands such as `git status`, `git diff`,
`git commit`, `git checkout`, or `git reset` unless the user explicitly asks for
Git or a tool requires Git-specific plumbing.

`pynix` is nanopynix's dogfooding consumer. It should depend on public
`nanopynix` APIs. If it needs a generally useful library capability, expose it
from nanopynix rather than importing a private implementation module. A narrow
private dependency is acceptable only when a redesign is not justified, and
must be explicitly documented at the import site.

pytest-agent auto-activates in this environment (it detects `CLAUDECODE` and
similar agent-harness env vars), so plain `pytest ...` invocations already
write full per-test detail — tracebacks, captured stdout/stderr/logs — to
`.pytest-agent/runs-NNNN/` regardless of what the terminal shows. There is no
need to pipe pytest through `tee`/`tail` to avoid losing output anymore; let
pytest's output go to stdout unfiltered. If a test fails, read its detail file
directly (path is printed in the run's "failed/errored" list, or found via
`.pytest-agent/history.jsonl`'s last line) rather than relying on the
terminal's minimal progress lines alone.

# Python coding conventions

- Use `from __future__ import annotations` in Python modules that define or use
  type annotations.
- Do not use string type hints such as `"Store"`. Use future annotations and
  `if TYPE_CHECKING:` imports instead.
- Keep imports at the top of the file. Lazy imports inside functions or methods
  are forbidden unless they are absolutely necessary to break a circular import
  cycle; prefer moving shared types to a neutral module over lazy imports.
- Import ordering:
  1. `from __future__ import annotations`
  2. standard library imports
  3. third-party imports
  4. local `nanopynix` imports
  5. `if TYPE_CHECKING:` block containing only type-only imports
  6. module constants
  7. code
- When re-exporting a name from another module, use the explicit re-export
  pattern `from module import Name as Name`. Consolidate related re-exports into
  one multi-line import block.
- Do not use `assert` statements outside `tests/`. For runtime validation, use
  explicit `if ...: raise ...`. To satisfy type checkers, prefer local variable
  aliasing or explicit `if value is None: raise ...` checks.
- Do not use `asyncio.get_event_loop()`. Use `asyncio.get_running_loop()` inside
  async code. For timestamps, use `time.monotonic()`.
- Keep a strong reference to background tasks created with
  `asyncio.create_task()`, for example in an instance `set` or `list`.
- Do not hide unexpected failures with `except Exception: pass`. Log unexpected
  exceptions. Use `contextlib.suppress(...)` only for expected ignored
  exceptions, with a comment explaining why they are safe to ignore.
- Every lint or type-checker suppression must name the specific rule and give
  an inline justification. Use the form
  `# type: ignore[rule-name] -- reason` or `# noqa: RULE -- reason`; do not
  use blanket or unexplained suppressions.
- Prefer anyio primitives (`anyio.Lock`/`Event`, memory object streams,
  `anyio.fail_after`/`move_on_after`, `anyio.create_task_group`,
  `anyio.open_process`, `anyio.to_thread`/`from_thread.BlockingPortal`) over
  raw `asyncio` equivalents in new code. Two documented, intentional
  exceptions exist: `_core/_nix_executor.py`'s `asyncio.wrap_future` call
  (interop with an already-running dedicated `concurrent.futures` thread —
  routing it through `anyio.to_thread` would spend a slot in anyio's shared
  capacity limiter for no benefit), and `asyncio.create_task()` used to host
  a `CancelScope`/`TaskGroup` (a plain `anyio.create_task_group()` or
  `anyio.from_thread.BlockingPortal`) whose `start()`/`close()` are invoked
  from different tasks (e.g. separate gRPC handler calls) — see
  `rpc/daemon/_supervisor.py` and `rpc/worker/_worker_primop.py`, since
  anyio's `CancelScope`/`TaskGroup` must be entered and exited by the same
  task.

# Banned patterns

- **In an async function, always use the async alternative when one exists —
  never a blocking sync call**, even in test code. `subprocess.run`/`Popen`/
  `os.system` → `anyio.open_process` (defaults `stdin`/`stdout`/`stderr` to
  `PIPE`, unlike asyncio — pass `None` explicitly to inherit the terminal).
  Blocking `pathlib.Path` I/O (`.read_text()`, `.write_text()`, `.exists()`,
  `.mkdir()`, etc.) → `anyio.Path`, same API, easy to miss since it doesn't
  look like a blocking call. Pure path manipulation with no filesystem access
  is still fine as plain `pathlib.Path`.

# Design notes

**Nix "stderr" = logging, not OS stderr**: Nix uses "stderr" terminology to
refer to `nix::Logger` log events. These already flow through the worker↔master
RPC pipe as `action: "msg"` / `action: "error"` events. Worker IPC uses only
stdin/stdout (JSON-RPC protocol); actual subprocess fd 2 inherits the parent.
Do NOT add a separate stderr pipe — it would be redundant and conflate Nix's
logging abstraction with OS-level stderr.

# Test Failure Discipline

Do not assume failing tests are unrelated, flaky, or pre-existing.

When a test fails after your changes, your default assumption must be:

> "My change caused or exposed this failure."

You may only call something a pre-existing issue after proving it with evidence.

## Required procedure for failing tests

When any test fails:

1. Re-run the exact failing test command to confirm the failure.
2. Inspect the failure carefully before making claims.
3. Check whether your recent changes could plausibly affect the failing behavior.
4. Use `git diff` to review every file you changed.
5. If you believe the failure is unrelated, verify that claim by either:
   - reverting your changes and showing the test still fails, or
   - running the same test on a clean baseline branch/commit, or
   - finding an existing failing CI/test record predating your work.

Without one of those checks, do not say:
- "This is pre-existing"
- "The tests are broken"
- "This is unrelated"
- "This is likely flaky"
- "The failure is outside the scope"

Instead say:

> "I have not proven this is unrelated yet. I will continue debugging under the assumption my change caused it."

## Pytest output discipline

pytest-agent auto-activates in this environment and already enforces this:
it refuses to run (exit code 2, before collecting anything) if it detects
its own stdout piped directly into `head`/`tail`/`grep`/`sed`/`awk`/etc., and
it always writes full per-test detail — tracebacks, captured stdout/stderr/
logs — to `.pytest-agent/runs-NNNN/` regardless of what the terminal shows.
So just let pytest's output go to the terminal unfiltered; there's no need to
route it through `tee` to avoid losing evidence anymore.

If pytest's minimal terminal output isn't enough to understand a failure,
read the detail file directly — its path is printed in the run's
"failed/errored" list at the end, or the test's log lives at
`.pytest-agent/runs-NNNN/<test file path>/<test name>.log` under the run
named in `.pytest-agent/history.jsonl`'s last line — rather than re-running
with ad hoc shell filtering.

## Never paper over failures

Do not modify tests just to match broken behavior.

Only update tests when:
- the intended behavior changed,
- the old test expectation is demonstrably obsolete,
- and the reason is explained clearly.

Do not weaken assertions, skip tests, delete coverage, or loosen error handling to make tests pass unless explicitly justified.

## Debugging expectations

Prefer small, evidence-driven steps:

- reproduce the failure
- isolate the smallest failing test
- inspect the relevant code path
- add temporary logging only if it helps identify the issue
- remove temporary logging before finishing
- make the smallest fix that addresses the root cause
- re-run the failing test
- then run the relevant broader test set

## Reporting failures

When reporting a test failure, include:

- the exact command run
- the exact failing test name
- the error or assertion message
- whether the failure was reproduced after your change
- why your fix addresses the root cause

If you believe a failure is pre-existing, include the proof.
A suspicion is not proof.
